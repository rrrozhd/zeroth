"""Actual loopback HTTP sidecar and Docker cancellation diagnostic."""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from zeroth.integrations.execution.runner import ExecutableUnitRunner
from zeroth.integrations.execution.sandbox import (
    SandboxManager,
    SandboxConfig,
    SandboxBackendMode,
    DockerSandboxConfig,
)
from zeroth.integrations.execution.sidecar_client import SandboxSidecarClient

out = Path(sys.argv[1])
base = os.environ["ZEROTH_TEST_DOCKER_IMAGE"]
tag = "zeroth-http-cancel-probe:" + uuid4().hex
secret = secrets.token_urlsafe(32)
requests = []
results = {}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()


def owned():
    # Ancestor matches include other aliases of this same image. Read IDs and
    # configured image names together so an unrelated disappearing container
    # cannot fail a later inspect between snapshots.
    rows = docker("ps", "-a", "--filter", f"ancestor={tag}", "--format", "{{.ID}} {{.Image}}")
    return [
        parts[0] for row in rows.splitlines() if len(parts := row.split()) == 2 and parts[1] == tag
    ]


class Client(SandboxSidecarClient):
    async def execute(self, request):
        requests.append(request)
        return await super().execute(request)


async def run(base_url):
    client = Client(base_url)
    manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.SIDECAR, docker=DockerSandboxConfig(container_name=tag)
        ),
        sidecar_client=client,
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    roots = []

    async def execute():
        with tempfile.TemporaryDirectory(prefix="zeroth-http-cancel-work-") as folder:
            root = Path(folder)
            roots.append(root)
            return await runner._execute_command(
                ["python", "-c", "import time; open('started','w').write('yes'); time.sleep(30)"],
                cwd=root,
                sandbox_root=root,
                relative_cwd=None,
                allowed_env_keys=[],
                overlay_env={},
                timeout_seconds=35,
            )

    task = asyncio.create_task(execute())
    try:
        for _ in range(100):
            if task.done():
                await task
                raise AssertionError("workload returned before positive startup control")
            active = owned()
            if any(
                subprocess.run(
                    ["docker", "exec", c, "test", "-f", "/workspace/started"], capture_output=True
                ).returncode
                == 0
                for c in active
            ):
                results["started_container_ids"] = active
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("workload never started")
        started = time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            results["caller_outcome"] = "cancelled"
        except Exception as exc:
            results["caller_outcome"] = type(exc).__name__
        results["cancellation_seconds"] = time.monotonic() - started
        results["workspace_exists_after_return"] = roots[0].exists()
        results["remaining_containers"] = [
            {"id": c, "state": json.loads(docker("inspect", c))[0]["State"]} for c in owned()
        ]
        results["execution_id"] = requests[0].execution_id
        response = httpx.get(
            base_url + "/executions/" + requests[0].execution_id,
            headers={"X-Zeroth-Sandbox-Secret": secret},
            timeout=5,
        )
        results["status_http"] = response.status_code
        results["server_status"] = response.json()
        results["network_exists"] = bool(
            docker(
                "network", "ls", "-q", "--filter", "name=zeroth-sandbox-" + requests[0].execution_id
            )
        )
        results["volume_exists"] = bool(
            docker("volume", "ls", "-q", "--filter", "name=zeroth-ws-" + requests[0].execution_id)
        )
        with tempfile.TemporaryDirectory(prefix="zeroth-http-reuse-") as folder:
            root = Path(folder)
            try:
                followup = await runner._execute_command(
                    ["python", "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
                    cwd=root,
                    sandbox_root=root,
                    relative_cwd=None,
                    allowed_env_keys=[],
                    overlay_env={},
                    input_text="http-stdin-roundtrip",
                    timeout_seconds=10,
                )
                results["reused_client"] = {
                    "returncode": followup.returncode,
                    "stdout": followup.stdout,
                }
            except Exception as exc:
                results["reused_client"] = {"error": type(exc).__name__, "message": str(exc)}
        (out / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2), flush=True)
    finally:
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task


server = None
previous = os.environ.get("ZEROTH_SANDBOX_SIDECAR_SECRET")
try:
    docker("tag", base, tag)
    with tempfile.TemporaryDirectory(prefix="zeroth-http-sidecar-spool-") as spool:
        with socket.socket() as sock, (out / "server.log").open("w") as log:
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            port = sock.getsockname()[1]
            env = dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                ZEROTH_SANDBOX_SIDECAR_SECRET=secret,
                ZEROTH_SIDECAR_WORKSPACE_SPOOL_DIR=spool,
                ZEROTH_SIDECAR_HELPER_IMAGE=tag,
            )
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "zeroth.integrations.sandbox.app:app",
                    "--fd",
                    str(sock.fileno()),
                    "--log-level",
                    "warning",
                ],
                pass_fds=(sock.fileno(),),
                env=env,
                stdout=log,
                stderr=log,
            )
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("sidecar server exited")
                try:
                    if httpx.get(url + "/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("sidecar server never ready")
            os.environ["ZEROTH_SANDBOX_SIDECAR_SECRET"] = secret
            asyncio.run(run(url))
finally:
    if previous is None:
        os.environ.pop("ZEROTH_SANDBOX_SIDECAR_SECRET", None)
    else:
        os.environ["ZEROTH_SANDBOX_SIDECAR_SECRET"] = previous
    if server is not None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    for c in owned():
        docker("rm", "--force", c)
    for request in requests:
        subprocess.run(
            ["docker", "network", "rm", "zeroth-sandbox-" + request.execution_id],
            capture_output=True,
        )
        subprocess.run(
            ["docker", "volume", "rm", "zeroth-ws-" + request.execution_id], capture_output=True
        )
    docker("image", "rm", tag)

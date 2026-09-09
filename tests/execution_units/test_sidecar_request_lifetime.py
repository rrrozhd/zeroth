"""Real HTTP coverage for transport ownership across sidecar callers."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
)
from zeroth.integrations.execution.sidecar_client import SandboxSidecarClient
from zeroth.integrations.http import factory


@pytest.fixture
def sidecar_http(monkeypatch):
    """Serve persistent HTTP connections and individually cancellable executions."""
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", "request-lifetime-test")
    executions: dict[str, threading.Event] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, body):
            assert self.headers["X-Zeroth-Sandbox-Secret"] == "request-lifetime-test"
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply({"status": "ok", "docker_available": True})

        def do_PUT(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply({})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/cancel"):
                executions[self.path.split("/")[2]].set()
                self.reply({})
                return
            request = json.loads(body)
            execution_id = request["execution_id"]
            status = "completed"
            if request["command"] == ["wait"]:
                release = executions[execution_id] = threading.Event()
                status = "cancelled" if release.wait(5) else "completed"
            self.reply({"execution_id": execution_id, "status": status, "returncode": 0})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", executions
    finally:
        for release in executions.values():
            release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def manager_for(client):
    return SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=client
    )


@pytest.mark.asyncio
async def test_one_manager_supports_async_and_repeated_sync_callers(sidecar_http):
    url, _ = sidecar_http
    client = SandboxSidecarClient(url, timeout=2)
    manager = manager_for(client)
    try:
        for _ in range(3):
            assert (await client.health()).docker_available
            result = await asyncio.to_thread(manager.run, ["true"], timeout_seconds=2)
            assert result.returncode == 0
        assert (await client.health()).docker_available
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connections", [1, 3])
async def test_cancellation_reaches_sidecar_with_all_execution_connections_busy(
    sidecar_http, monkeypatch, connections
):
    url, executions = sidecar_http
    # Vary the bound to test the failure class without opening 100 sockets in
    # every unit run. The separate review probe exercises the production bound.
    monkeypatch.setattr(factory, "DEFAULT_LIMITS", httpx.Limits(max_connections=connections))
    client = SandboxSidecarClient(url, timeout=2)
    manager = manager_for(client)
    environment = manager.prepare_environment(allowed_env_keys=[], overlay={})
    tasks = [
        asyncio.create_task(
            manager.run_via_sidecar_async(
                command=["wait"], input_text=None, timeout_seconds=5, environment=environment
            )
        )
        for _ in range(connections)
    ]
    try:
        async with asyncio.timeout(2):
            while len(executions) != connections:
                await asyncio.sleep(0.001)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(tasks[0]), timeout=1)
        assert sum(release.is_set() for release in executions.values()) == 1
        assert all(not task.done() for task in tasks[1:])
    finally:
        for release in executions.values():
            release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()

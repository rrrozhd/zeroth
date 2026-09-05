"""Cancellation must retain execution ownership through worker cleanup."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

from zeroth.integrations.execution.runner import ExecutableUnitRunner
from zeroth.integrations.execution.sandbox import (
    DockerSandboxConfig,
    SandboxBackendMode,
    SandboxBackendUnavailableError,
    SandboxConfig,
    SandboxManager,
    SandboxStrictnessMode,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_repeated_cancellation_retains_workspace_until_worker_exits(
    monkeypatch, cleanup_fails
) -> None:
    runner = ExecutableUnitRunner()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    roots = []

    def worker(*args, **kwargs):
        root = args[3]
        roots.append(root)
        started.set()
        try:
            assert release.wait(5), "test did not release worker"
            assert root.exists(), "workspace removed before worker cleanup"
            if cleanup_fails:
                raise SandboxBackendUnavailableError("cleanup failed")
            return None
        finally:
            finished.set()

    monkeypatch.setattr(runner, "_run_with_prepared_environment", worker)

    async def execute():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            await runner._execute_command(
                ["true"],
                cwd=root,
                sandbox_root=root,
                relative_cwd=None,
                allowed_env_keys=[],
                overlay_env={},
                timeout_seconds=2,
            )

    task = asyncio.create_task(execute())
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "cancellation escaped before worker cleanup"
        assert roots[0].exists()
        release.set()
        expected = SandboxBackendUnavailableError if cleanup_fails else asyncio.CancelledError
        with pytest.raises(expected):
            await task
        assert finished.is_set()
        assert not roots[0].exists()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, SandboxBackendUnavailableError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_delay", [0.0, 2.1])
async def test_real_docker_cancellation_waits_for_removal_before_workspace_cleanup(
    startup_delay: float,
) -> None:
    image = os.environ.get("ZEROTH_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("set ZEROTH_TEST_DOCKER_IMAGE for real Docker cancellation")
    token = uuid4().hex
    tag = f"zeroth-cancellation-test:{token}"
    template = f"zeroth-cancellation-template-{token}"
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()

    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()

    def command_runner(command, **kwargs):
        if command[1] == "create":
            # Cancellation ordering must not depend on Docker starting within 2 s.
            threading.Event().wait(startup_delay)
        if command[1] == "rm":
            cleanup_entered.set()
            assert release_cleanup.wait(5), "test did not release container removal"
        return subprocess.run(command, **kwargs)

    tagged = False
    task = None
    try:
        docker("tag", image, tag)
        tagged = True
        template_id = docker("run", "-d", "--name", template, tag, "sleep", "120")
        manager = SandboxManager(
            config=SandboxConfig(
                backend=SandboxBackendMode.DOCKER,
                strictness_mode=SandboxStrictnessMode.STRICT,
                docker=DockerSandboxConfig(container_name=template),
            ),
            command_runner=command_runner,
        )
        runner = ExecutableUnitRunner(sandbox_manager=manager)
        roots = []

        async def execute():
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                roots.append(root)
                await runner._execute_command(
                    [
                        "python",
                        "-c",
                        "import time; open('started','w').write('yes'); time.sleep(60)",
                    ],
                    cwd=root,
                    sandbox_root=root,
                    relative_cwd=None,
                    allowed_env_keys=[],
                    overlay_env={},
                    timeout_seconds=30,
                )

        task = asyncio.create_task(execute())
        async with asyncio.timeout(10):
            while not (roots and (roots[0] / "started").exists()):
                if task.done():
                    await task
                    pytest.fail("workload exited without its startup marker")
                await asyncio.sleep(0.01)
        assert roots and (roots[0] / "started").exists(), "workload did not start"
        task.cancel()
        for _ in range(100):
            if cleanup_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert cleanup_entered.is_set(), "cancellation did not promptly stop execution"
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert roots[0].exists()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert not roots[0].exists()
        assert docker("ps", "-aq", "--no-trunc", "--filter", f"ancestor={tag}").split() == [
            template_id
        ]
        assert docker("inspect", "-f", "{{.State.Running}}", template) == "true"
    finally:
        release_cleanup.set()
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if tagged:
            for container_id in docker("ps", "-aq", "--filter", f"ancestor={tag}").split():
                if docker("inspect", "-f", "{{.Config.Image}}", container_id) == tag:
                    docker("rm", "--force", container_id)
            docker("image", "rm", tag)


@pytest.mark.asyncio
async def test_local_cancellation_stops_process_before_returning(tmp_path) -> None:
    import sys

    processes = []

    def launch(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process

    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.LOCAL), process_factory=launch
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    task = asyncio.create_task(
        runner._execute_command(
            [
                sys.executable,
                "-c",
                "import time, subprocess, sys; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); "
                "open('started','w').write(str(child.pid)); time.sleep(3)",
            ],
            cwd=tmp_path,
            sandbox_root=tmp_path,
            relative_cwd=None,
            allowed_env_keys=[],
            overlay_env={},
            timeout_seconds=5,
        )
    )
    try:
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "started").exists()
        task.cancel()
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.done(), "cancelled local execution kept running"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(processes) == 1
        assert processes[0].poll() is not None
        if os.name == "posix":
            child_pid = int((tmp_path / "started").read_text())
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            assert not state or state.startswith("Z"), "local child survived cancellation"
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_race", [False, True])
@pytest.mark.parametrize("cancel_failure", [False, True])
async def test_sidecar_cancellation_reaches_owned_execution(
    tmp_path, registration_race, cancel_failure
) -> None:
    import httpx

    from zeroth.integrations.sandbox.models import SidecarExecuteResponse

    started = threading.Event()
    release = threading.Event()
    requests = []
    cancelled_ids = []

    class Client:
        async def upload_workspace(self, *_args):
            return None

        async def execute(self, request):
            requests.append(request)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return SidecarExecuteResponse(
                execution_id=request.execution_id,
                status="cancelled",
                returncode=-9,
                stdout="",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
            )

        async def cancel(self, execution_id):
            cancelled_ids.append(execution_id)
            if registration_race and len(cancelled_ids) == 1:
                response = httpx.Response(
                    404, request=httpx.Request("POST", "http://sidecar/cancel")
                )
                response.raise_for_status()
            release.set()
            if cancel_failure:
                response = httpx.Response(
                    503, request=httpx.Request("POST", "http://sidecar/cancel")
                )
                response.raise_for_status()

    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=Client()
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    task = asyncio.create_task(
        runner._execute_command(
            ["sleep", "30"],
            cwd=tmp_path,
            sandbox_root=tmp_path,
            relative_cwd=None,
            allowed_env_keys=[],
            overlay_env={},
            timeout_seconds=5,
        )
    )
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.done(), "cancellation never reached sidecar"
        expected = SandboxBackendUnavailableError if cancel_failure else asyncio.CancelledError
        with pytest.raises(expected):
            await task
        assert cancelled_ids == [requests[0].execution_id] * (2 if registration_race else 1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, SandboxBackendUnavailableError):
            await task

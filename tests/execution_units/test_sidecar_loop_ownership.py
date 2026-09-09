"""Sidecar HTTP stays on the caller's loop while filesystem work runs off-loop."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from zeroth.integrations.execution import sidecar_client as sidecar_client_module
from zeroth.integrations.execution.runner import ExecutableUnitRunner
from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
)
from zeroth.integrations.execution.sidecar_client import SandboxSidecarClient
from zeroth.integrations.sandbox.models import SidecarExecuteResponse


def _response(execution_id: str) -> SidecarExecuteResponse:
    return SidecarExecuteResponse(
        execution_id=execution_id,
        status="completed",
        returncode=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.01,
        timed_out=False,
    )


@pytest.mark.asyncio
async def test_runner_reaches_the_sidecar_on_its_own_loop(tmp_path: Path) -> None:
    """The upload and execute HTTP calls stay on the runner's event loop."""
    calls: list[tuple[str, int, asyncio.AbstractEventLoop]] = []

    class Client:
        async def upload_workspace(self, workspace_id, tar):
            calls.append(("upload", threading.get_ident(), asyncio.get_running_loop()))

        async def execute(self, request):
            calls.append(("execute", threading.get_ident(), asyncio.get_running_loop()))
            return _response(request.execution_id)

    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=Client()
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    result = await runner._execute_command(
        ["true"],
        cwd=tmp_path,
        sandbox_root=tmp_path,
        relative_cwd=None,
        allowed_env_keys=[],
        overlay_env={},
        timeout_seconds=5,
    )

    assert result.backend == "sidecar" and result.stdout == "ok"
    assert [name for name, _, _ in calls] == ["upload", "execute"]
    assert {thread for _, thread, _ in calls} == {threading.get_ident()}
    assert {loop for _, _, loop in calls} == {asyncio.get_running_loop()}


@pytest.mark.asyncio
async def test_failed_requests_release_their_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", "pool-secret")
    handed_out: list[httpx.AsyncClient] = []
    factory = sidecar_client_module.governed_async_client

    async def recording_factory(**kwargs):
        client = await factory(**kwargs)
        handed_out.append(client)
        return client

    monkeypatch.setattr(sidecar_client_module, "governed_async_client", recording_factory)
    client = SandboxSidecarClient("http://127.0.0.1:9", timeout=0.5)
    try:
        for _ in range(2):
            with pytest.raises(httpx.TransportError):
                await client.health()
            assert all(transport.is_closed for transport in handed_out)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["_sidecar_request", "_sidecar_result"])
async def test_filesystem_work_allows_loop_progress(monkeypatch, tmp_path, phase):
    client = AsyncMock()
    client.execute.return_value = _response("filesystem")
    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=client
    )
    operation = getattr(manager, phase)
    loop = asyncio.get_running_loop()

    def checked_operation(*args, **kwargs):
        advanced = threading.Event()
        loop.call_soon_threadsafe(advanced.set)
        assert advanced.wait(1), "application loop blocked by filesystem work"
        return operation(*args, **kwargs)

    monkeypatch.setattr(manager, phase, checked_operation)
    environment = manager.prepare_environment(allowed_env_keys=[], overlay={})
    result = await manager.run_via_sidecar_async(
        command=["true"],
        input_text=None,
        timeout_seconds=5,
        environment=environment,
        sandbox_root=tmp_path,
    )
    assert result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["_sidecar_request", "_sidecar_result"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cancelled_filesystem_work_retains_workspace_and_errors(monkeypatch, phase, fails):
    client = AsyncMock()
    client.execute.return_value = _response("filesystem").model_copy(
        update={"output_file_b64": base64.b64encode(b'{"ok": true}').decode()}
    )
    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=client
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    operation = getattr(manager, phase)
    started, release = threading.Event(), threading.Event()
    roots = []
    failure = OSError("filesystem unavailable")

    def blocked_operation(*args, **kwargs):
        started.set()
        assert release.wait(2), "filesystem work blocked the cancelling caller"
        assert roots[0].exists(), "workspace removed before filesystem work finished"
        if fails:
            raise failure
        return operation(*args, **kwargs)

    monkeypatch.setattr(manager, phase, blocked_operation)

    async def run():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            roots.append(root)
            await runner._execute_command(
                ["true"],
                cwd=root,
                sandbox_root=root,
                relative_cwd=None,
                allowed_env_keys=[],
                overlay_env={},
                timeout_seconds=5,
                capture_output_file="out.json",
            )

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "cancellation escaped before filesystem work finished"
        assert roots[0].exists()
        release.set()
        with pytest.raises(OSError if fails else asyncio.CancelledError) as caught:
            await task
        if fails:
            assert caught.value is failure
        assert not roots[0].exists()
        if phase == "_sidecar_request":
            client.upload_workspace.assert_not_awaited()
            client.execute.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_loop_shutdown_does_not_remove_the_workspace_under_the_output_writer(monkeypatch) -> None:
    """A serving loop shutting down must not tear the workspace out from under a writer."""
    client = AsyncMock()
    client.execute.return_value = _response("shutdown").model_copy(
        update={"output_file_b64": base64.b64encode(b'{"ok": true}').decode()}
    )
    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR), sidecar_client=client
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    original = manager._sidecar_result
    entered, release = threading.Event(), threading.Event()
    observed: dict[str, object] = {}

    def slow_result(*args, **kwargs):
        entered.set()
        release.wait(2)
        observed["root_present_at_write"] = observed["root"].exists()
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_sidecar_result", slow_result)

    async def execute() -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observed["root"] = root
            await runner._execute_command(
                ["true"],
                cwd=root,
                sandbox_root=root,
                relative_cwd=None,
                allowed_env_keys=[],
                overlay_env={},
                timeout_seconds=5,
                capture_output_file="out.json",
            )

    async def main() -> None:
        asyncio.create_task(execute())
        await asyncio.to_thread(entered.wait, 2)
        asyncio.get_running_loop().call_later(0.1, release.set)

    asyncio.run(main())

    assert observed["root_present_at_write"] is True
    assert not observed["root"].exists()

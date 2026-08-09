from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteRequest
from zeroth.integrations.execution.sandbox import SandboxPolicyViolationError


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _CompletedProcess:
    returncode = None

    def __init__(self) -> None:
        self.stdout = _ChunkStream([b"stdout", b"-overflow"])
        self.stderr = _ChunkStream([b"stderr", b"-overflow"])
        self.stdin = None

    async def communicate(self, input=None):  # noqa: A002
        raise AssertionError("communicate() would buffer hostile output without a bound")

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def test_sidecar_bounds_stdout_and_stderr_with_truncation_metadata(monkeypatch) -> None:
    executor = SidecarExecutor(max_output_bytes=6)
    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_CompletedProcess()),
    )

    response = await executor.execute(
        SidecarExecuteRequest(execution_id="bounded", image="python", command=["run"])
    )

    assert response.stdout == "stdout"
    assert response.stderr == "stderr"
    assert response.stdout_truncated is True
    assert response.stderr_truncated is True


class _TimeoutProcess:
    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    async def communicate(self, input=None):  # noqa: A002
        await asyncio.Future()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return -9


async def test_sidecar_timeout_kills_and_waits_for_child(monkeypatch) -> None:
    process = _TimeoutProcess()
    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    response = await executor.execute(
        SidecarExecuteRequest(
            execution_id="timeout", image="python", command=["sleep"], timeout_seconds=0.01
        )
    )

    assert response.timed_out is True
    assert process.killed is True
    assert process.waited is True


class _ActiveProcess:
    returncode = None

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.killed = False
        self.waited = False

    async def communicate(self, input=None):  # noqa: A002
        self.started.set()
        await self.stopped.wait()
        return b"partial", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stopped.set()

    async def wait(self) -> int:
        self.waited = True
        await self.stopped.wait()
        return -9


async def test_sidecar_cancel_stops_active_child_persists_status_and_cleans_network(
    monkeypatch,
) -> None:
    process = _ActiveProcess()
    docker_commands: list[tuple[str, ...]] = []

    async def run_cmd(*args: str):
        docker_commands.append(args)
        return b"", b""

    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_ActiveProcess()),
    )
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    execution = asyncio.create_task(
        executor.execute(
            SidecarExecuteRequest(execution_id="cancel", image="python", command=["sleep"])
        )
    )
    await process.started.wait()

    await executor.cancel("cancel")
    assert ("docker", "network", "rm", "zeroth-sandbox-cancel") in docker_commands
    response = await execution
    status = await executor.get_status("cancel")

    assert process.killed is True
    assert process.waited is True
    assert response.status == "cancelled"
    assert status is not None and status.status == "cancelled"


async def test_sidecar_cancel_does_not_rewrite_completed_execution(monkeypatch) -> None:
    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_CompletedProcess()),
    )
    completed = await executor.execute(
        SidecarExecuteRequest(execution_id="done", image="python", command=["run"])
    )

    await executor.cancel("done")

    status = await executor.get_status("done")
    assert completed.status == "completed"
    assert status is not None and status.status == "completed"


async def test_external_task_cancellation_reaps_child_and_cleans_state(monkeypatch) -> None:
    process = _ActiveProcess()
    commands: list[tuple[str, ...]] = []

    async def run_cmd(*args: str):
        commands.append(args)
        return b"", b""

    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    task = asyncio.create_task(
        executor.execute(
            SidecarExecuteRequest(execution_id="external", image="python", command=["sleep"])
        )
    )
    await process.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed and process.waited
    assert commands.count(("docker", "network", "rm", "zeroth-sandbox-external")) == 1
    assert executor._states == {}
    status = await executor.get_status("external")
    assert status is not None and status.status == "cancelled"


async def test_cancel_before_spawn_prevents_child_creation(monkeypatch) -> None:
    network_entered = asyncio.Event()
    release_network = asyncio.Event()
    commands: list[tuple[str, ...]] = []

    async def run_cmd(*args: str):
        commands.append(args)
        if args[1:3] == ("network", "create"):
            network_entered.set()
            await release_network.wait()
        return b"", b""

    spawn = AsyncMock(return_value=_ActiveProcess())
    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec", spawn
    )
    execution = asyncio.create_task(
        executor.execute(SidecarExecuteRequest(execution_id="pre", image="python", command=["run"]))
    )
    await network_entered.wait()
    cancellation = asyncio.create_task(executor.cancel("pre"))
    await asyncio.sleep(0)
    release_network.set()

    response = await execution
    await cancellation

    assert response.status == "cancelled"
    spawn.assert_not_awaited()
    assert commands.count(("docker", "network", "rm", "zeroth-sandbox-pre")) == 1
    assert executor._states == {}


async def test_duplicate_active_execution_id_is_rejected_before_side_effects(monkeypatch) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    commands: list[tuple[str, ...]] = []

    async def run_cmd(*args: str):
        commands.append(args)
        if args[1:3] == ("network", "create"):
            entered.set()
            await release.wait()
        return b"", b""

    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_ActiveProcess()),
    )
    first = asyncio.create_task(
        executor.execute(
            SidecarExecuteRequest(execution_id="duplicate", image="python", command=["run"])
        )
    )
    await entered.wait()

    with pytest.raises(SandboxPolicyViolationError, match="execution request"):
        await executor.execute(
            SidecarExecuteRequest(execution_id="duplicate", image="python", command=["other"])
        )

    assert len([call for call in commands if call[1:3] == ("network", "create")]) == 1
    cancellation = asyncio.create_task(executor.cancel("duplicate"))
    await asyncio.sleep(0)
    release.set()
    await cancellation
    await first
    assert executor._states == {}

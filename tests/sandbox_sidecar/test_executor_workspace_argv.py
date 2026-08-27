"""Argv and lifecycle pins for the ZER-37 executor workspace path.

Follows the fake-run harness of ``test_executor_argv.py``: docker never runs.
Volume create/populate/workload/capture/rm sequences are recorded through the
staging command seams, while the re-authoring split runs for real against a
spooled tar on disk.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteRequest
from zeroth.integrations.sandbox.staging import (
    WorkspaceValidationCode,
    WorkspaceValidationError,
)

HELPER_IMAGE = "helper.example/populate:1"
VOLUME_SOURCE_PATTERN = re.compile(r"^zeroth-ws-[A-Za-z0-9_.-]+$")


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):  # noqa: A002 - match asyncio API
        return b"ok", b""


class _ActiveProcess:
    returncode = None

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def communicate(self, input=None):  # noqa: A002
        self.started.set()
        await self.stopped.wait()
        return b"partial", b""

    def kill(self) -> None:
        self.returncode = -9
        self.stopped.set()

    async def wait(self) -> int:
        await self.stopped.wait()
        return -9


class _FakeStore:
    def __init__(self, spool: Path) -> None:
        self.spool = spool
        self.claims: list[str] = []

    async def claim(self, workspace_id: str) -> Path:
        self.claims.append(workspace_id)
        return self.spool


def _spool_tar(tmp_path: Path, files: dict[str, bytes]) -> Path:
    spool = tmp_path / "workspace-spool.tar"
    with tarfile.open(spool, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return spool


def _workspace_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes] | None = None,
) -> tuple[SidecarExecutor, list[tuple[tuple[str, ...], Path | None]], dict[str, list[str]]]:
    executor = SidecarExecutor()
    executor._helper_image = HELPER_IMAGE
    spool = _spool_tar(
        tmp_path,
        files or {"main.py": b"print('hi')", "cfg/settings.json": b"{}", "data/ro/blob": b"x"},
    )
    executor.workspace_store = _FakeStore(spool)
    staging_calls: list[tuple[tuple[str, ...], Path | None]] = []

    async def run_staging_cmd(*args: str, stdin_path: Path | None = None):
        staging_calls.append((args, stdin_path))
        return b"", b""

    captured: dict[str, list[str]] = {}

    async def fake_exec(*cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(executor, "_run_staging_cmd", run_staging_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec", fake_exec
    )
    return executor, staging_calls, captured


def _request(**overrides) -> SidecarExecuteRequest:
    defaults = {
        "execution_id": "ws-exec",
        "image": "python:3.12",
        "command": ["python", "main.py"],
        "workspace_id": "ws-1",
    }
    defaults.update(overrides)
    return SidecarExecuteRequest(**defaults)


def _volume_rm_calls(
    staging_calls: list[tuple[tuple[str, ...], Path | None]],
) -> list[tuple[str, ...]]:
    return [args for args, _ in staging_calls if args[1:3] == ("volume", "rm")]


async def test_workspace_argv_mounts_named_volumes_with_hardening_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, captured = _workspace_executor(tmp_path, monkeypatch)

    response = await executor.execute(_request(read_only_paths=["cfg", "data/ro"]))

    assert response.status == "completed"
    cmd = captured["cmd"]
    # Hardening pins survive the workspace path.
    assert "--read-only" in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert "--mount" not in cmd
    assert "--volume" not in cmd
    # Volume tokens: main volume read-write at /workspace, each read-only
    # prefix as its own :ro volume at its mountpoint.
    volume_values = [cmd[index + 1] for index, token in enumerate(cmd) if token == "-v"]
    assert volume_values == [
        "zeroth-ws-ws-exec:/workspace",
        "zeroth-ws-ws-exec-ro0:/workspace/cfg:ro",
        "zeroth-ws-ws-exec-ro1:/workspace/data/ro:ro",
    ]
    # Every -v source is a docker volume NAME -- never a host path.
    for value in volume_values:
        assert VOLUME_SOURCE_PATTERN.fullmatch(value.split(":")[0])


async def test_workspace_staging_sequence_creates_populates_then_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)

    await executor.execute(_request(read_only_paths=["cfg"]))

    creates = [args for args, _ in staging_calls if args[1:3] == ("volume", "create")]
    assert creates == [
        ("docker", "volume", "create", "zeroth-ws-ws-exec"),
        ("docker", "volume", "create", "zeroth-ws-ws-exec-ro0"),
    ]
    populates = [(args, stdin) for args, stdin in staging_calls if args[1] == "run"]
    assert [args[:2] for args, _ in populates] == [("docker", "run")] * 2
    for args, stdin_path in populates:
        # The populate helper is hardened, offline, and fed a spooled
        # sidecar-authored tar on stdin.
        assert "--network=none" in args
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--security-opt") + 1] == "no-new-privileges"
        assert "-i" in args
        assert args[-7:] == (HELPER_IMAGE, "tar", "-x", "-f", "-", "-C", "/w")
        assert stdin_path is not None
    mounted = [args[args.index("-v") + 1] for args, _ in populates]
    assert mounted == ["zeroth-ws-ws-exec:/w", "zeroth-ws-ws-exec-ro0:/w"]
    assert _volume_rm_calls(staging_calls) == [
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec-ro0"),
    ]
    # The claimed spool is deleted once finalize runs.
    assert not executor.workspace_store.spool.exists()


async def test_no_workspace_request_never_touches_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, captured = _workspace_executor(tmp_path, monkeypatch)

    await executor.execute(
        SidecarExecuteRequest(execution_id="plain", image="python:3.12", command=["true"])
    )

    assert staging_calls == []
    assert executor.workspace_store.claims == []
    assert "-v" not in captured["cmd"]


async def test_capture_output_file_rides_the_execute_response_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)
    capture_calls: list[tuple[tuple[str, ...], int]] = []

    async def run_capture_cmd(*args: str, max_bytes: int):
        capture_calls.append((args, max_bytes))
        return b"hello world", False, 0

    monkeypatch.setattr(executor, "_run_capture_cmd", run_capture_cmd)

    response = await executor.execute(_request(capture_output_file="out/result.json"))

    assert response.output_file_b64 == base64.b64encode(b"hello world").decode("ascii")
    assert response.output_file_truncated is False
    ((args, max_bytes),) = capture_calls
    assert args[:3] == ("docker", "run", "--rm")
    assert "--network=none" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("-v") + 1] == "zeroth-ws-ws-exec:/w:ro"
    assert args[-3:] == (HELPER_IMAGE, "cat", "/w/out/result.json")
    assert max_bytes == executor._max_output_file_bytes + 1
    # The persisted record never replays the payload.
    status = await executor.get_status("ws-exec")
    assert status is not None
    assert status.output_file_b64 is None
    assert status.output_file_truncated is False


async def test_capture_overflow_drops_payload_and_reports_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, _, _ = _workspace_executor(tmp_path, monkeypatch)

    async def run_capture_cmd(*args: str, max_bytes: int):
        return b"x" * max_bytes, True, 0

    monkeypatch.setattr(executor, "_run_capture_cmd", run_capture_cmd)

    response = await executor.execute(_request(capture_output_file="big.bin"))

    assert response.output_file_b64 is None
    assert response.output_file_truncated is True
    status = await executor.get_status("ws-exec")
    assert status is not None
    assert status.output_file_b64 is None
    assert status.output_file_truncated is True


async def test_missing_output_file_yields_no_payload_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, _, _ = _workspace_executor(tmp_path, monkeypatch)

    async def run_capture_cmd(*args: str, max_bytes: int):
        return b"", False, 1  # cat: no such file

    monkeypatch.setattr(executor, "_run_capture_cmd", run_capture_cmd)

    response = await executor.execute(_request(capture_output_file="absent.txt"))

    assert response.status == "completed"
    assert response.output_file_b64 is None
    assert response.output_file_truncated is False


async def test_finalize_removes_volumes_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)

    class _TimeoutProcess:
        returncode = None

        async def communicate(self, input=None):  # noqa: A002
            await asyncio.Future()

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_TimeoutProcess()),
    )

    response = await executor.execute(_request(timeout_seconds=0.01))

    assert response.timed_out is True
    assert _volume_rm_calls(staging_calls) == [
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
    ]
    assert not executor.workspace_store.spool.exists()


async def test_finalize_removes_volumes_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)
    process = _ActiveProcess()
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    execution = asyncio.create_task(executor.execute(_request()))
    await process.started.wait()

    await executor.cancel("ws-exec")
    response = await execution

    assert response.status == "cancelled"
    assert _volume_rm_calls(staging_calls) == [
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
    ]
    assert not executor.workspace_store.spool.exists()


async def test_finalize_removes_volumes_on_workload_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=RuntimeError("spawn failed")),
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        await executor.execute(_request())

    assert _volume_rm_calls(staging_calls) == [
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
    ]
    assert not executor.workspace_store.spool.exists()
    status = await executor.get_status("ws-exec")
    assert status is not None and status.status == "failed"


async def test_volume_removal_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, _, _ = _workspace_executor(tmp_path, monkeypatch)
    attempts: list[tuple[str, ...]] = []

    async def flaky_staging_cmd(*args: str, stdin_path: Path | None = None):
        if args[1:3] == ("volume", "rm"):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("volume busy")
        return b"", b""

    monkeypatch.setattr(executor, "_run_staging_cmd", flaky_staging_cmd)

    response = await executor.execute(_request())

    assert response.status == "completed"
    assert attempts == [
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
        ("docker", "volume", "rm", "-f", "zeroth-ws-ws-exec"),
    ]


async def test_unknown_workspace_fails_before_any_docker_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)

    async def claim(workspace_id: str) -> Path:
        raise WorkspaceValidationError(WorkspaceValidationCode.WORKSPACE_UNKNOWN)

    executor.workspace_store.claim = claim
    run_cmd = AsyncMock(return_value=(b"", b""))
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await executor.execute(_request())

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_UNKNOWN
    run_cmd.assert_not_awaited()  # no network was created
    assert staging_calls == []  # no volume was created
    status = await executor.get_status("ws-exec")
    assert status is not None and status.status == "failed"


async def test_invalid_workspace_charset_fails_before_claim_or_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, staging_calls, _ = _workspace_executor(tmp_path, monkeypatch)

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await executor.execute(
            SidecarExecuteRequest(
                execution_id="bad/exec-id",
                image="python:3.12",
                command=["true"],
                workspace_id="ws-1",
            )
        )

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_ID_INVALID
    assert executor.workspace_store.claims == []
    assert staging_calls == []
    # Nothing registered: the id was refused at the boundary.
    assert await executor.get_status("bad/exec-id") is None
    assert executor._states == {}


async def test_missing_store_refuses_workspace_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SidecarExecutor()
    run_cmd = AsyncMock(return_value=(b"", b""))
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await executor.execute(_request())

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_UNKNOWN
    run_cmd.assert_not_awaited()
    assert executor._states == {}

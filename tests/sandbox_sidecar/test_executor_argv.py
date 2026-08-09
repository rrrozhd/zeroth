"""Regression test for the sidecar docker argv (audit B11)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteRequest


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):  # noqa: A002 - match asyncio API
        return b"ok", b""


@pytest.mark.parametrize("network_access", [False, True])
async def test_execute_argv_has_exactly_one_network_flag(monkeypatch, network_access) -> None:
    # B11: the executor attaches the container to its own per-execution network
    # via `--network={name}`. If the resource-flag builder ALSO emits a
    # `--network` token (it did, because network_access was a non-None bool),
    # docker aborts with exit 125 on conflicting network modes. The assembled
    # argv must carry exactly one --network token regardless of network_access.
    captured: dict[str, list[str]] = {}

    async def _fake_exec(*cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    executor = SidecarExecutor()
    # Skip the real `docker network create/rm` calls.
    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    request = SidecarExecuteRequest(
        execution_id="exec-1",
        image="python:3.12",
        command=["echo", "hi"],
        cpu_cores=1.0,
        memory_mb=256,
        max_processes=8,
        network_access=network_access,
    )
    await executor.execute(request)

    cmd = captured["cmd"]
    network_tokens = [tok for tok in cmd if tok.startswith("--network")]
    assert network_tokens == ["--network=zeroth-sandbox-exec-1"]
    # The resource flags still carry the non-network constraints.
    assert "--cpus" in cmd
    assert "--memory" in cmd
    assert "--pids-limit" in cmd


async def test_execute_argv_applies_shared_hardening_without_host_mounts(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    async def _fake_exec(*cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", AsyncMock(return_value=(b"", b"")))
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    await executor.execute(
        SidecarExecuteRequest(
            execution_id="hardened",
            image="python:3.12",
            command=["python", "-c", "pass"],
            network_access=False,
        )
    )

    cmd = captured["cmd"]
    assert "--read-only" in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert cmd[cmd.index("--tmpfs") + 1] == "/tmp"
    assert "-v" not in cmd
    assert "--volume" not in cmd
    assert "--mount" not in cmd
    assert [token for token in cmd if token.startswith("--network")] == [
        "--network=zeroth-sandbox-hardened"
    ]

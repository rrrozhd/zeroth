"""Regression test for the sidecar docker argv (audit B11)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteRequest
from zeroth.integrations.execution.sandbox import (
    SandboxPolicyViolationError,
    validate_docker_image_reference,
)


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


@pytest.mark.parametrize(
    "image",
    [
        "--volume=/host:/host",
        "--privileged",
        " python:3.12",
        "python:3.12\n--privileged",
        "UPPER/repository:tag",
        "repository//image:tag",
        "repository@sha256:abc",
        "repository@sha512:abc",
        "repository::tag",
        "registry.example.com:65536/repository",
        "[::1:5000/team/image:tag",
        "[not-ipv6]:5000/team/image:tag",
        "[fe80::1%eth0]:5000/team/image:tag",
        "[::1]:port/team/image:tag",
        "[::1]:/team/image:tag",
        "[::1]junk/team/image:tag",
        "a" * 256 + ":tag",
        "repository:" + "T" * 129,
        "repository@md5:" + "a" * 32,
    ],
)
async def test_sidecar_rejects_noncanonical_image_before_docker_spawn(
    monkeypatch, image: str
) -> None:
    executor = SidecarExecutor()
    run_cmd = AsyncMock()
    spawn = AsyncMock(return_value=_FakeProc())
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec", spawn
    )

    with pytest.raises(SandboxPolicyViolationError, match="image reference") as exc_info:
        await executor.execute(
            SidecarExecuteRequest(execution_id="image-policy", image=image, command=["python:3.12"])
        )

    assert image not in str(exc_info.value)
    run_cmd.assert_not_awaited()
    spawn.assert_not_awaited()


@pytest.mark.parametrize(
    "image",
    [
        "python",
        "python:3.12",
        "localhost:5000/team/image:tag",
        "registry.example.com:443/openai/team/zeroth:Release_1",
        "ghcr.io/openai/zeroth:release@sha256:" + "a" * 64,
        "ghcr.io/openai/zeroth@sha384:" + "b" * 96,
        "ghcr.io/openai/zeroth@sha512:" + "c" * 128,
        "[::1]:5000/team/image:tag",
        "[2001:db8::1]/team/image:UPPER",
        "registry.example.com:80/a/b/c:Tag",
        "xn--bcher-kva.example/team/image:tag",
        "Registry.EXAMPLE.com/team/image:Tag",
        "a" * 255 + ":Release@sha256:" + "d" * 64,
    ],
)
def test_canonical_docker_image_reference_acceptance_corpus(image: str) -> None:
    assert validate_docker_image_reference(image) == image


@pytest.mark.parametrize(
    ("request_kwargs", "internal"),
    [({}, True), ({"network_access": False}, True), ({"network_access": True}, False)],
)
async def test_sidecar_network_is_internal_unless_explicitly_authorized(
    monkeypatch, request_kwargs, internal
) -> None:
    network_calls: list[tuple[str, ...]] = []

    async def run_cmd(*args: str):
        network_calls.append(args)
        return b"", b""

    executor = SidecarExecutor()
    monkeypatch.setattr(executor, "_run_cmd", run_cmd)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc()),
    )

    await executor.execute(
        SidecarExecuteRequest(
            execution_id="network-policy",
            image="python:3.12",
            command=["echo"],
            **request_kwargs,
        )
    )

    create = next(call for call in network_calls if call[1:3] == ("network", "create"))
    assert ("--internal" in create) is internal

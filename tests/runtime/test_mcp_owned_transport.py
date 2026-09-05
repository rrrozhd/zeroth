"""Ownership, cancellation and secrecy of the private Docker stdio lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp import StdioServerParameters
from pydantic import ValidationError

from zeroth.runtime.agents import _mcp_docker_transport as transport
from zeroth.runtime.agents.mcp import MCPServerConfig, RegisteredMCPServerConfig
from zeroth.runtime.agents.mcp_isolation import MCPDockerIsolationConfig, MCPProcessIsolator


def test_cleanup_ownership_is_not_a_serialized_server_field() -> None:
    profile = MCPProcessIsolator(MCPDockerIsolationConfig(image="sha256:" + "a" * 64))
    config = profile.isolate(RegisteredMCPServerConfig(name="test", command="true", grants=[]))
    assert set(config.model_dump()) == {"name", "command", "args", "env"}
    assert config._docker_workload is not None
    assert config.model_copy(deep=True)._docker_workload == config._docker_workload
    assert MCPServerConfig.model_validate(config.model_dump())._docker_workload is None
    with pytest.raises(ValidationError):
        MCPServerConfig(name="author", command="docker", _docker_workload=config._docker_workload)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_failure", [False, True])
async def test_only_acknowledged_workloads_are_removed(monkeypatch, creation_failure) -> None:
    calls = []
    attached = []
    workload = transport.DockerStdioWorkload("docker", "owned", ("create", "--name", "owned"))

    async def control(workload, args, environment, operation):
        calls.append(operation)
        if operation == "creation" and creation_failure:
            raise RuntimeError("creation failed")
        if operation == "cleanup":
            raise RuntimeError("cleanup failed")

    @asynccontextmanager
    async def stdio(params):
        attached.append(params.args)
        yield None

    monkeypatch.setattr(transport, "_control", control)
    with pytest.raises(RuntimeError, match="creation" if creation_failure else "cleanup"):
        async with transport.owned_docker_stdio(
            workload, StdioServerParameters(command="docker"), stdio
        ):
            pass
    assert calls == (["creation"] if creation_failure else ["creation", "cleanup"])
    assert attached == (
        [] if creation_failure else [["start", "--attach", "--interactive", "owned"]]
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_owned_removal() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup():
        entered.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(transport._finish_cleanup(cleanup()))
    await entered.wait()
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_control_failure_redacts_private_arguments_and_daemon_output(monkeypatch) -> None:
    import traceback

    secret = "private-mcp-control-canary"
    workload = transport.DockerStdioWorkload("docker", "owned", ("create", secret))

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", secret.encode()

    async def launch(*args, **kwargs):
        return Process()

    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(RuntimeError) as caught:
        await transport._control(workload, workload.create_args, {}, "creation")
    assert secret not in "".join(traceback.format_exception(caught.value))

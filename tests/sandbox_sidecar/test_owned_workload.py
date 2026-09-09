"""Sidecar cancellation must remove the daemon workload, not only its CLI."""

from __future__ import annotations

import asyncio

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteRequest


class Process:
    def __init__(self):
        self.returncode = None
        self.done = asyncio.Event()

    async def communicate(self, input=None):
        await self.done.wait()
        return b"", b""

    def kill(self):
        self.returncode = -9
        self.done.set()

    async def wait(self):
        await self.done.wait()
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_cancel_removes_owned_container_before_reporting_terminal(
    monkeypatch, cleanup_failure
):
    executor = SidecarExecutor()
    commands = []
    started = asyncio.Event()
    proc = Process()

    async def control(*args):
        commands.append(args)
        if args[1:3] == ("rm", "--force") and cleanup_failure:
            raise RuntimeError("container removal failed")
        return b"", b""

    async def launch(*args, **kwargs):
        commands.append(args)
        started.set()
        return proc

    monkeypatch.setattr(executor, "_run_cmd", control)
    monkeypatch.setattr(
        "zeroth.integrations.sandbox.executor.asyncio.create_subprocess_exec", launch
    )
    task = asyncio.create_task(
        executor.execute(
            SidecarExecuteRequest(
                execution_id="public-id", image="python:3.12", command=["sleep", "30"]
            )
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    try:
        if cleanup_failure:
            with pytest.raises(RuntimeError, match="removal"):
                await executor.cancel("public-id")
            with pytest.raises(RuntimeError, match="removal"):
                await task
            assert (await executor.get_status("public-id")).status == "failed"
        else:
            assert await executor.cancel("public-id")
            await task
            create = next(c for c in commands if c[1] == "create")
            name = create[create.index("--name") + 1]
            assert "public-id" not in name
            removal = ("docker", "rm", "--force", name)
            assert removal in commands
            assert commands.index(removal) < commands.index(
                ("docker", "network", "rm", "zeroth-sandbox-public-id")
            )
            assert (await executor.get_status("public-id")).status == "cancelled"
    finally:
        proc.kill()
        if not task.done():
            await task
        else:
            task.exception()


@pytest.mark.asyncio
async def test_creation_failure_does_not_echo_command_or_environment(monkeypatch):
    import traceback

    canary = "private-creation-test-canary"
    executor = SidecarExecutor()
    commands = []

    async def control(*args):
        commands.append(args)
        if args[1] == "create":
            raise RuntimeError(f"Command {args} failed: {canary}")
        return b"", b""

    monkeypatch.setattr(executor, "_run_cmd", control)
    with pytest.raises(RuntimeError) as caught:
        await executor.execute(
            SidecarExecuteRequest(
                execution_id="creation-failure",
                image="python:3.12",
                command=["echo", canary],
                environment={"PRIVATE": canary},
            )
        )
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert all(command[1] not in {"start", "rm"} for command in commands)
    assert (await executor.get_status("creation-failure")).status == "failed"

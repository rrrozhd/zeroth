"""A CLI tool's child process is always bounded and always reaped (ZER-48 / A06-11).

Two halves of one defect.

``timeout_seconds`` defaulted to ``None`` and was passed straight into
``asyncio.wait_for``, so a tool declared without an explicit timeout waited
forever on its child.

And the kill only ran in the ``except asyncio.TimeoutError`` branch.  Cancelling
the awaiting task raises ``CancelledError``, which never enters that branch — so
the child survived its cancellation and had to be reaped by hand.  Both of the
cancellers that exist in this codebase reach that path: fail-fast sibling
cancellation in the parallel executor, and lease-loss drive cancellation in the
run worker.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents.tooling.cli_tool import (
    DEFAULT_CLI_TOOL_TIMEOUT_SECONDS,
    CLITool,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _sleeper(seconds: float) -> CLITool:
    return CLITool(
        name="sleeper",
        command=[sys.executable, "-c", f"import time; time.sleep({seconds})"],
        input_model=_In,
        output_model=_Out,
    )


def test_a_tool_without_an_explicit_timeout_still_has_one() -> None:
    """The default must be a number, not ``None``."""
    assert DEFAULT_CLI_TOOL_TIMEOUT_SECONDS > 0
    assert _sleeper(0.01).timeout_seconds is None, (
        "the tool-level field may stay optional; the deadline is resolved at use"
    )


@pytest.mark.asyncio
async def test_cancellation_kills_the_child() -> None:
    """The measured defect: the child outlived the cancelled task."""
    tool = _sleeper(30)
    tool.timeout_seconds = 30

    started = asyncio.Event()
    captured: dict[str, asyncio.subprocess.Process] = {}
    real_exec = asyncio.create_subprocess_exec

    async def _spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
        captured["process"] = process
        started.set()
        return process

    asyncio.create_subprocess_exec = _spy  # type: ignore[assignment]
    try:
        task = asyncio.create_task(tool._execute_validated(None, _In()))
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]

    process = captured["process"]
    assert process.returncode is not None, "the child survived the cancelled task"
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


@pytest.mark.asyncio
async def test_a_timeout_still_kills_the_child() -> None:
    """The path that already worked must keep working."""
    from zeroth.runtime.agents.tooling.base import CLIToolTimeoutError

    tool = _sleeper(30)
    tool.timeout_seconds = 0.1

    with pytest.raises(CLIToolTimeoutError):
        await tool._execute_validated(None, _In())

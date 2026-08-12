"""SidecarExecutor.cancel() must distinguish unknown from known executions.

A07-11: the sidecar's cancel route always returned ``{"status": "cancelled"}``
regardless of whether the execution existed, unlike its sibling ``get_status``
route which 404s. The route can only mirror that behavior if the executor
itself reports whether the execution was known.
"""

from __future__ import annotations

import pytest

from zeroth.integrations.sandbox.executor import SidecarExecutor
from zeroth.integrations.sandbox.models import SidecarExecuteResponse


@pytest.mark.asyncio
async def test_cancel_unknown_execution_returns_false() -> None:
    """An execution_id never submitted is reported as not found."""
    executor = SidecarExecutor()

    found = await executor.cancel("never-submitted")

    assert found is False


@pytest.mark.asyncio
async def test_cancel_already_completed_execution_returns_true() -> None:
    """An execution that finished (and left ``_states``) is still known."""
    executor = SidecarExecutor()
    executor._executions["done-1"] = SidecarExecuteResponse(
        execution_id="done-1",
        status="completed",
        returncode=0,
    )

    found = await executor.cancel("done-1")

    assert found is True

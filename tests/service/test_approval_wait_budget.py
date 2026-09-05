"""Approval's best-effort wait includes time spent reading durable state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zeroth.contracts.governed import RunStatus
from zeroth.service.api import approval_api


@pytest.mark.parametrize("observe_running", [False, True])
async def test_wait_budget_bounds_slow_reads_and_returns_latest_view(
    monkeypatch, observe_running
):
    monkeypatch.setattr(approval_api, "_WORKER_WAIT_SECONDS", 0.15, raising=False)
    original = SimpleNamespace(run_id="run-1", status=RunStatus.PENDING)
    latest = SimpleNamespace(run_id="run-1", status=RunStatus.RUNNING)
    cancelled = asyncio.Event()
    calls = 0

    async def get(_run_id):
        nonlocal calls
        calls += 1
        if observe_running and calls == 1:
            return latest
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bootstrap = SimpleNamespace(run_repository=SimpleNamespace(get=get))
    result = await asyncio.wait_for(
        approval_api._wait_for_worker_run(bootstrap, original), timeout=0.8
    )
    assert result is (latest if observe_running else original)
    assert cancelled.is_set()


async def test_wait_returns_terminal_durable_view():
    original = SimpleNamespace(run_id="run-1", status=RunStatus.PENDING)
    terminal = SimpleNamespace(run_id="run-1", status=RunStatus.COMPLETED)
    bootstrap = SimpleNamespace(run_repository=SimpleNamespace(get=AsyncMock(return_value=terminal)))
    assert await approval_api._wait_for_worker_run(bootstrap, original) is terminal


async def test_wait_does_not_hide_repository_timeout():
    original = SimpleNamespace(run_id="run-1", status=RunStatus.PENDING)
    bootstrap = SimpleNamespace(
        run_repository=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError("database")))
    )
    with pytest.raises(TimeoutError, match="database"):
        await approval_api._wait_for_worker_run(bootstrap, original)


async def test_wait_preserves_caller_cancellation():
    original = SimpleNamespace(run_id="run-1", status=RunStatus.PENDING)
    bootstrap = SimpleNamespace(
        run_repository=SimpleNamespace(get=AsyncMock(side_effect=asyncio.CancelledError))
    )
    with pytest.raises(asyncio.CancelledError):
        await approval_api._wait_for_worker_run(bootstrap, original)

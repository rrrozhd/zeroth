"""Regression for the exact-head load latency evidence failure."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_accepted_latency_ends_at_the_http_response_not_terminal_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.load_release import workload_probe

    service = SimpleNamespace(
        deployment=SimpleNamespace(deployment_ref="deployment", tenant_id="tenant"),
        run_repository=SimpleNamespace(count_pending=AsyncMock(return_value=0)),
    )
    target = workload_probe.Target(
        workload_probe.Scope(service, None, {}, "slow-script", "replica-1"), None
    )
    monkeypatch.setattr(workload_probe, "_observed_worker", AsyncMock(return_value="worker"))
    monkeypatch.setattr(
        workload_probe,
        "_settle_run",
        AsyncMock(return_value=[{"state": "completed", "at_ms": 9000.0, "run_id": "run"}]),
    )
    elapsed = iter((101.05, 110.0))
    monkeypatch.setattr(workload_probe.time, "perf_counter", lambda: next(elapsed))
    monkeypatch.setattr(workload_probe.time, "process_time", lambda: 1.005)

    row = await workload_probe._accepted_row(
        target,
        "sustained",
        1,
        100.0,
        101.0,
        1.0,
        SimpleNamespace(status_code=202, json=lambda: {"run_id": "run"}),
    )

    assert row["finished_at_ms"] == pytest.approx(1050.0)
    assert row["latency_ms"] == pytest.approx(50.0)
    assert row["lifecycle"][-1]["at_ms"] == 9000.0

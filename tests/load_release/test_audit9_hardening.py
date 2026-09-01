"""Regression for ZER-33 request-response load isolation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_terminal_observation_is_rate_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-band status reads must not become the load generator."""
    from tests.load_release import workload_probe

    responses = iter(
        (
            SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"status": "running"},
            ),
            SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"status": "succeeded"},
            ),
        )
    )
    sleeps: list[float] = []

    async def get(*_args, **_kwargs):
        return next(responses)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    target = workload_probe.Target(
        SimpleNamespace(secrets={"operator": "secret"}),
        SimpleNamespace(get=get),
    )
    monkeypatch.setattr(workload_probe.asyncio, "sleep", sleep)

    terminal = await workload_probe._settle_run(
        target,
        "sustained",
        1,
        "run-1",
        workload_probe.time.perf_counter(),
    )

    assert terminal[0]["state"] == "completed"
    assert sleeps == [0.05]


def test_idle_worker_polling_cannot_eclipse_the_scheduled_workload() -> None:
    """The probe's workers must not manufacture a database load test."""
    from tests.load_release import workload_probe

    class Worker:
        poll_interval = 1.0

        async def _execute_leased_run(self, run_id, **_kwargs):
            return run_id

    service = SimpleNamespace(
        orchestrator=SimpleNamespace(agent_runners={}),
        worker=Worker(),
    )

    workload_probe.install_runner(service, "slow-script")

    assert service.worker.poll_interval == 0.04


@pytest.mark.parametrize("status_code", (202, 429))
@pytest.mark.asyncio
async def test_response_processing_does_not_hold_the_request_inflight_slot(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    from tests.load_release import workload_probe

    class Slot:
        held = False

        async def __aenter__(self):
            self.held = True

        async def __aexit__(self, *_args):
            self.held = False

    slot = Slot()
    target = workload_probe.Target(
        SimpleNamespace(
            secrets={"operator": "test-operator-key"},
            service=SimpleNamespace(worker=SimpleNamespace(worker_id="worker")),
        ),
        SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(status_code=status_code, headers={}))
        ),
    )

    async def accepted(*_args, **_kwargs):
        assert slot.held is False
        return {"request_id": "accepted"}

    def rejected(*_args, **_kwargs):
        assert slot.held is False
        return {"request_id": "rejected"}

    monkeypatch.setattr(workload_probe, "_accepted_row", accepted)
    monkeypatch.setattr(workload_probe, "_row", rejected)

    row = await workload_probe._measure(target, "overload", 1, 0.0, slot)

    assert row == {"request_id": "accepted" if status_code == 202 else "rejected"}

"""Regression for ZER-33 request-response load isolation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


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

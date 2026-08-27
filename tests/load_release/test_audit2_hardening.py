"""Regressions for ZER-33 AUDIT-2 evidence fidelity."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_baseline_binds_every_run_to_a_measured_base_receipt() -> None:
    baseline = json.loads((ROOT / "release/load/baseline-v1.json").read_text())
    source = baseline["source"]

    assert len(source["run_receipts"]) == source["sample_run_count"] == 3
    assert {
        (receipt["commit"], receipt["package_version"]) for receipt in source["run_receipts"]
    } == {(source["commit"], source["package_version"])}
    assert {receipt["tree"] for receipt in source["run_receipts"]} == {source["tree"]}
    assert [receipt["observation_digest"] for receipt in source["run_receipts"]] == source[
        "run_digests"
    ]
    assert all(
        receipt["environment"] == baseline["environment"] for receipt in source["run_receipts"]
    )


def test_fairness_fails_closed_for_starved_deployment_replica_and_worker() -> None:
    from release.load.measurements import recompute

    rows = []
    for sequence in range(20):
        tenant = f"tenant-{sequence % 2}"
        deployment = "deployment-b" if sequence == 19 else "deployment-a"
        replica = "replica-b" if sequence == 19 else "replica-a"
        worker = "worker-b" if sequence == 19 else "worker-a"
        run_id = f"run-{sequence}"
        rows.append(
            {
                "request_id": f"request-{sequence}",
                "profile": "sustained",
                "tenant_id": tenant,
                "deployment_ref": deployment,
                "replica": replica,
                "worker": worker,
                "surface": "slow-script",
                "fault": None,
                "status_code": 202,
                "retry_after_seconds": None,
                "started_at_ms": float(sequence),
                "finished_at_ms": float(sequence + 1),
                "latency_ms": 1.0,
                "queue_depth": 0,
                "cpu_percent": 1.0,
                "memory_bytes": 1,
                "lifecycle": [
                    {"state": "accepted", "at_ms": 0.0, "run_id": run_id},
                    {"state": "completed", "at_ms": 1.0, "run_id": run_id},
                ],
            }
        )

    measured = recompute(rows, {"sustained": {}})["sustained"]

    assert measured["tenant_fairness"] == 1.0
    assert measured["deployment_fairness"] < 0.9
    assert measured["replica_fairness"] < 0.9
    assert measured["worker_fairness"] < 0.9


@pytest.mark.asyncio
async def test_worker_loss_emits_separate_accepted_and_rejected_request_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.load_release import fault_probe

    service = SimpleNamespace(
        deployment=SimpleNamespace(tenant_id="tenant-1", deployment_ref="worker-loss"),
        worker=SimpleNamespace(worker_id="worker-1"),
    )

    async def scoped(*_args, **_kwargs):
        return service, {"operator": "secret"}

    async def submit(_service, _secrets, _started, states):
        states.extend(
            [
                {"state": "accepted", "at_ms": 1.0, "run_id": "run-1"},
                {"state": "rejected", "at_ms": 2.0},
            ]
        )
        return "run-1", (_started, _started + 0.1), (_started + 0.2, _started + 0.3), 503, 1

    @asynccontextmanager
    async def serving(_app):
        yield "http://unused"

    monkeypatch.setattr(fault_probe, "_scoped_service", scoped)
    monkeypatch.setattr(fault_probe, "_submit_under_worker_loss", submit)
    monkeypatch.setattr(fault_probe, "_wait_terminal", lambda *_args: _completed())
    monkeypatch.setattr(fault_probe, "create_app", lambda _service: object())
    monkeypatch.setattr(fault_probe, "serve", serving)
    monkeypatch.setattr(fault_probe.httpx, "AsyncClient", _Client)

    rows = await fault_probe.worker_loss(
        None, (SimpleNamespace(deployment=service.deployment), None, {})
    )

    assert len(rows) == 2
    assert len({row["request_id"] for row in rows}) == 2
    accepted, rejected = rows
    assert {event["state"] for event in accepted["lifecycle"]} >= {"accepted", "completed"}
    assert "rejected" not in {event["state"] for event in accepted["lifecycle"]}
    assert rejected["status_code"] == 503
    assert {event["state"] for event in rejected["lifecycle"]} == {"rejected"}


async def _completed() -> str:
    return "completed"


class _Response:
    status_code = 202
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"run_id": "run-1", "status": "running"}


class _Client:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *_args, **_kwargs) -> _Response:
        return _Response()


@pytest.mark.asyncio
async def test_cancellation_requires_the_returned_product_state() -> None:
    from tests.load_release.workload_probe import Scope, Target, _settle_run

    scope = Scope(
        service=SimpleNamespace(deployment=SimpleNamespace(deployment_ref="deployment")),
        auth=None,
        secrets={"admin": "secret"},
        surface="slow-script",
        replica="replica-1",
    )

    with pytest.raises(AssertionError, match="cancellation"):
        await _settle_run(Target(scope, _Client()), "overload", 0, "run-1", 0.0)


@pytest.mark.asyncio
async def test_drain_is_recorded_only_after_the_service_has_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.load_release import fault_probe

    stopped = False

    @asynccontextmanager
    async def serving(_app):
        nonlocal stopped
        yield "http://unused"
        stopped = True

    class States(list):
        def append(self, item):
            if item.get("state") == "draining":
                assert not stopped, "drain evidence followed the observed service stop"
            super().append(item)

    monkeypatch.setattr(fault_probe, "serve", serving)
    monkeypatch.setattr(fault_probe, "create_app", lambda _service: object())
    monkeypatch.setattr(fault_probe.httpx, "AsyncClient", _Client)

    states = States()
    worker = SimpleNamespace(
        lease_manager=SimpleNamespace(current_holder=_worker_holder), worker_id="worker-1"
    )
    deployment = SimpleNamespace(deployment_ref="deployment", tenant_id="tenant", workspace_id=None)
    await fault_probe._submit_for_restart(
        SimpleNamespace(worker=worker, deployment=deployment),
        {"operator": "secret"},
        0.0,
        states,
    )

    assert states[-1]["state"] == "draining"


async def _worker_holder(*_args, **_kwargs) -> str:
    return "worker-1"


def test_documented_reproduction_is_complete_and_pinned() -> None:
    page = (ROOT / "docs/how-to/deployment/release-gates.md").read_text()

    assert "--artifact" in page
    assert "ZEROTH_LOAD_RUNTIME_IMAGE=" in page
    assert "ZEROTH_LOAD_POSTGRES_VERSION=" in page
    assert "ZEROTH_LOAD_REDIS_VERSION=" in page
    assert "--cpus 2 --memory 8g" in page

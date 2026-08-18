"""Regressions for ZER-33 AUDIT-3 evidence fidelity."""

from __future__ import annotations

import copy
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _workload_row(deployment: str, replica: str, worker: str, completed: bool) -> dict:
    lifecycle = [{"state": "accepted", "at_ms": 0.0, "run_id": f"{deployment}-{replica}"}]
    if completed:
        lifecycle.append({"state": "completed", "at_ms": 1.0, "run_id": f"{deployment}-{replica}"})
    return {
        "profile": "sustained",
        "deployment_ref": deployment,
        "replica": replica,
        "worker": worker,
        "fault": None,
        "started_at_ms": 0.0,
        "finished_at_ms": 1.0,
        "latency_ms": 1.0,
        "status_code": 202,
        "queue_depth": 0,
        "cpu_percent": 1.0,
        "memory_bytes": 1,
        "tenant_id": "tenant",
        "lifecycle": lifecycle,
    }


def test_replica_and_worker_fairness_are_scoped_to_deployment() -> None:
    from release.load.measurements import recompute

    rows = []
    for deployment, active_replica in (
        ("deployment-a", "replica-1"),
        ("deployment-b", "replica-2"),
    ):
        for replica in ("replica-1", "replica-2"):
            rows.extend(
                _workload_row(
                    deployment,
                    replica,
                    "worker-1",
                    replica == active_replica,
                )
                for _ in range(10)
            )

    measured = recompute(rows, {"sustained": {}})["sustained"]

    assert measured["replica_fairness"] == 0.5
    assert measured["worker_fairness"] == 0.5


@pytest.mark.asyncio
async def test_workload_records_the_worker_that_claimed_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.load_release import workload_probe

    class LeaseManager:
        async def current_holder(self, _run_id, *, tenant_id=None, workspace_id=None):
            assert tenant_id == "tenant"
            assert workspace_id is None
            return "executor-worker"

    class Repository:
        async def count_pending(self, *_args):
            return 0

    service = SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="deployment", tenant_id="tenant", workspace_id=None
        ),
        worker=SimpleNamespace(worker_id="submitter-worker", lease_manager=LeaseManager()),
        run_repository=Repository(),
    )
    target = workload_probe.Target(
        workload_probe.Scope(service, None, {}, "slow-script", "replica-1"), None
    )

    async def settled(*_args, **_kwargs):
        return [{"state": "completed", "at_ms": 2.0, "run_id": "run-1"}]

    monkeypatch.setattr(workload_probe, "_settle_run", settled)
    response = SimpleNamespace(status_code=202, json=lambda: {"run_id": "run-1"})
    now = time.perf_counter()

    row = await workload_probe._accepted_row(
        target, "sustained", 1, now - 1, now - 0.01, time.process_time(), response
    )

    assert row["worker"] == "executor-worker"


@pytest.mark.asyncio
async def test_completed_claim_remains_attributable_after_lease_release() -> None:
    from tests.load_release import workload_probe

    class Worker:
        worker_id = "executor-worker"
        poll_interval = 1

        async def _execute_leased_run(self, run_id, **_kwargs):
            return run_id

    service = SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="deployment", tenant_id="tenant", workspace_id=None
        ),
        orchestrator=SimpleNamespace(agent_runners={}),
        worker=Worker(),
    )
    workload_probe.install_runner(service, "slow-script")

    assert await service.worker._execute_leased_run("run-completed") == "run-completed"
    assert await workload_probe._observed_worker(service, "run-completed") == "executor-worker"


@pytest.mark.asyncio
async def test_worker_observation_allows_the_existing_run_settlement_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.load_release import workload_probe

    observed_times = iter((0.0, 0.0, 2.1))
    monkeypatch.setattr(workload_probe.time, "perf_counter", lambda: next(observed_times))

    class LeaseManager:
        calls = 0

        async def current_holder(self, _run_id, **_scope):
            self.calls += 1
            return None if self.calls == 1 else "delayed-worker"

    service = SimpleNamespace(
        deployment=SimpleNamespace(tenant_id="tenant", workspace_id=None),
        worker=SimpleNamespace(lease_manager=LeaseManager()),
    )

    assert await workload_probe._observed_worker(service, "run-delayed") == "delayed-worker"


def test_fault_rows_can_retain_their_exact_request_window_and_serving_worker() -> None:
    from tests.load_release.backend_fault_probe import fault_row

    row = fault_row(
        "worker-loss",
        "failing-script",
        100.0,
        [{"state": "rejected", "at_ms": 200.0}],
        status=503,
        retry_after=1,
        request_started=100.1,
        request_finished=100.2,
        worker_id="serving-worker",
        recovered=False,
    )

    assert row["started_at_ms"] == pytest.approx(100.0)
    assert row["finished_at_ms"] == pytest.approx(200.0)
    assert row["latency_ms"] == pytest.approx(100.0)
    assert row["worker"] == "serving-worker"


@pytest.mark.asyncio
async def test_drain_signal_is_recorded_before_the_server_has_stopped(
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
                assert not stopped, "drain evidence was emitted after the server stopped"
            super().append(item)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"run_id": "run-1"},
            )

    monkeypatch.setattr(fault_probe, "serve", serving)
    monkeypatch.setattr(fault_probe, "create_app", lambda _service: object())
    monkeypatch.setattr(fault_probe.httpx, "AsyncClient", lambda **_kwargs: Client())
    worker = SimpleNamespace(
        lease_manager=SimpleNamespace(current_holder=_worker_holder), worker_id="worker-1"
    )
    deployment = SimpleNamespace(deployment_ref="deployment", tenant_id="tenant", workspace_id=None)

    await fault_probe._submit_for_restart(
        SimpleNamespace(worker=worker, deployment=deployment),
        {"operator": "secret"},
        time.perf_counter(),
        States(),
    )


async def _worker_holder(*_args, **_kwargs) -> str:
    return "worker-1"


def test_every_429_or_503_requires_rejection_evidence_and_retry_after() -> None:
    from release.load.report import evidence_errors, load_profiles
    from tests.load_release.test_report import PROFILES, _rows

    rows = copy.deepcopy(_rows())
    rejected = next(row for row in rows if row["status_code"] in {429, 503})
    rejected["lifecycle"] = [
        event for event in rejected["lifecycle"] if event["state"] != "rejected"
    ]
    rejected["retry_after_seconds"] = None

    errors = evidence_errors(rows, load_profiles(PROFILES))

    assert any("rejection event" in error for error in errors)
    assert any("Retry-After" in error for error in errors)


def test_baseline_receipts_are_atomically_bound_to_the_executed_source() -> None:
    baseline = json.loads((ROOT / "release/load/baseline-v1.json").read_text())

    assert (ROOT / "release/load/receipt.py").is_file()
    assert all(
        receipt.get("source_digest", "").startswith("sha256:")
        and receipt.get("generated_at", "").endswith("Z")
        for receipt in baseline["source"]["run_receipts"]
    )


def test_documented_reproduction_pins_every_container_to_linux_arm64() -> None:
    page = (ROOT / "docs/how-to/deployment/release-gates.md").read_text()

    assert page.count("--platform linux/arm64") >= 3

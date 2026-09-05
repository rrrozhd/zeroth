"""ZER-33 workload semantics across fairness, faults, and overload."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "release/load/profiles-v1.json"


def _profiles() -> dict:
    return json.loads(PROFILES.read_text(encoding="utf-8"))


async def test_real_product_fairness_fault_and_overload_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RC job owns real PostgreSQL, Redis, HTTP, and lifecycle probes."""
    monkeypatch.chdir(tmp_path)
    output = os.environ.get("ZEROTH_LOAD_OBSERVATIONS")
    postgres_dsn = os.environ.get("ZEROTH_LOAD_POSTGRES_DSN")
    redis_url = os.environ.get("ZEROTH_LOAD_REDIS_URL")
    if not output and not postgres_dsn and not redis_url:
        pytest.skip("real load-release services are not configured")
    assert output and postgres_dsn and redis_url, "load-release service config is partial"

    # Webhook and other control-plane mutations are required to write signed
    # audit records. Give this isolated candidate run an ephemeral key instead
    # of weakening that invariant or depending on a repository secret.
    monkeypatch.setenv("SIGNING_DEPLOYMENT", secrets.token_hex(32))

    from tests.load_release.product_probe import collect_product_observations
    from release.load.report import evidence_errors

    if diagnostic_output := os.environ.get("ZEROTH_LOAD_DIAGNOSTICS"):
        from tests.load_release.approval_diagnostics import install

        install(monkeypatch, ROOT / diagnostic_output, postgres_dsn)

    profiles = _profiles()
    rows = await collect_product_observations(
        profiles, postgres_dsn=postgres_dsn, redis_url=redis_url
    )
    assert evidence_errors(rows, profiles) == []
    path = ROOT / output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_fairness_uses_completed_product_work_not_submitted_intent() -> None:
    from release.load.report import recompute

    rows = []
    for sequence in range(12):
        tenant = "tenant-1" if sequence < 10 else "tenant-2"
        run_id = f"run-{sequence}"
        rows.append(
            {
                "request_id": f"request-{sequence}",
                "profile": "sustained",
                "tenant_id": tenant,
                "deployment_ref": f"{tenant}-deployment-1",
                "replica": "replica-1",
                "worker": "worker-1",
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

    fairness = recompute(rows, _profiles()["profiles"])["sustained"]["tenant_fairness"]

    assert fairness < _profiles()["thresholds"]["rules"]["tenant_fairness_minimum"]["minimum"]


def test_cancelled_work_does_not_count_as_completed_fairness() -> None:
    from release.load.report import recompute

    rows = []
    for tenant in ("tenant-1", "tenant-2"):
        for sequence in range(2):
            row = _accounting_row(f"{tenant}-done-{sequence}", tenant, "replica-1", None)
            row["lifecycle"].append(
                {"state": "completed", "at_ms": 1.0, "run_id": f"{tenant}-done-{sequence}"}
            )
            rows.append(row)
    for sequence in range(8):
        run_id = f"tenant-1-cancelled-{sequence}"
        row = _accounting_row(run_id, "tenant-1", "replica-1", None)
        row["lifecycle"].append({"state": "cancelled", "at_ms": 1.0, "run_id": run_id})
        rows.append(row)

    fairness = recompute(rows, _profiles()["profiles"])["overload"]["tenant_fairness"]

    assert fairness == 1.0


@pytest.mark.parametrize(
    "fault",
    [
        "database-contention",
        "redis-loss",
        "worker-loss",
        "service-restart",
        "network-delay",
        "downstream-throttling",
    ],
)
def test_fault_evidence_requires_observed_automatic_recovery(fault: str) -> None:
    from release.load.report import evidence_errors

    row = {
        "request_id": "request-1",
        "profile": "overload",
        "tenant_id": "tenant-1",
        "deployment_ref": "tenant-1-deployment-1",
        "replica": "replica-1",
        "worker": "worker-1",
        "surface": "slow-script",
        "fault": fault,
        "status_code": 503,
        "retry_after_seconds": 1,
        "started_at_ms": 0.0,
        "finished_at_ms": 1.0,
        "latency_ms": 1.0,
        "queue_depth": 1,
        "cpu_percent": 1.0,
        "memory_bytes": 1,
        "lifecycle": [
            {"state": "fault-injected", "at_ms": 0.0},
            {"state": "rejected", "at_ms": 1.0},
        ],
    }

    errors = evidence_errors([row], _profiles())

    assert any(fault in error and "automatic recovery" in error for error in errors)


@pytest.mark.parametrize(
    ("fault", "required_state"),
    [
        ("database-contention", "coordination-timeout"),
        ("redis-loss", "artifact-restored"),
        ("worker-loss", "worker-replaced"),
        ("service-restart", "service-started"),
        ("network-delay", "transport-restored"),
        ("downstream-throttling", "delivery-retried"),
    ],
)
def test_fault_evidence_requires_the_fault_specific_recovery_observation(
    fault: str, required_state: str
) -> None:
    from release.load.report import evidence_errors

    row = _accounting_row("accepted-1", "tenant-1", "replica-1", fault)
    row["status_code"] = 503
    row["retry_after_seconds"] = 1
    row["lifecycle"].extend(
        [
            {"state": "fault-injected", "at_ms": 0.0},
            {"state": "rejected", "at_ms": 0.5},
            {"state": "cancelled", "at_ms": 0.75, "run_id": "accepted-1"},
            {"state": "recovered", "at_ms": 1.0, "repair": "automatic"},
        ]
    )

    errors = evidence_errors([row], _profiles())

    assert any(fault in error and required_state in error for error in errors)


def test_service_restart_recovery_requires_a_successful_durable_run() -> None:
    from release.load.report import evidence_errors

    row = _accounting_row("accepted-1", "tenant-1", "replica-1", "service-restart")
    row["surface"] = "slow-script"
    row["lifecycle"].extend(
        [
            {"state": "draining", "at_ms": 1.0, "run_id": "accepted-1"},
            {"state": "service-stopped", "at_ms": 2.0},
            {"state": "service-started", "at_ms": 3.0},
            {"state": "failed", "at_ms": 4.0, "run_id": "accepted-1"},
            {"state": "recovered", "at_ms": 5.0, "repair": "automatic"},
        ]
    )

    errors = evidence_errors([row], _profiles())

    assert any("service-restart" in error and "successful durable run" in error for error in errors)


@pytest.mark.parametrize("status", [200, 202, 400, 500, 504])
def test_overload_rejections_are_only_429_or_503_with_retry_after(status: int) -> None:
    from release.load.report import evidence_errors

    row = {
        "request_id": "request-1",
        "profile": "overload",
        "tenant_id": "tenant-1",
        "deployment_ref": "tenant-1-deployment-1",
        "replica": "replica-1",
        "worker": "worker-1",
        "surface": "slow-script",
        "fault": "network-delay",
        "status_code": status,
        "retry_after_seconds": None,
        "started_at_ms": 0.0,
        "finished_at_ms": 1.0,
        "latency_ms": 1.0,
        "queue_depth": 1,
        "cpu_percent": 1.0,
        "memory_bytes": 1,
        "lifecycle": [
            {"state": "fault-injected", "at_ms": 0.0},
            {"state": "rejected", "at_ms": 1.0},
            {"state": "recovered", "at_ms": 2.0, "repair": "automatic"},
        ],
    }

    errors = evidence_errors([row], _profiles())

    assert any("429 or 503" in error for error in errors)
    assert any("Retry-After" in error for error in errors)


def test_drain_and_cancellation_must_terminally_account_for_every_accepted_id() -> None:
    from release.load.report import evidence_errors

    drain = _accounting_row("accepted-1", "tenant-1", "replica-1", "service-restart")
    drain["lifecycle"].extend(
        [
            {"state": "draining", "at_ms": 1.0, "run_id": "accepted-1"},
            {"state": "recovered", "at_ms": 2.0, "repair": "automatic"},
        ]
    )
    cancel = _accounting_row("accepted-2", "tenant-2", "replica-2", None)
    cancel["lifecycle"].append({"state": "cancel-requested", "at_ms": 1.0, "run_id": "accepted-2"})
    rows = [drain, cancel]

    errors = evidence_errors(rows, _profiles())

    assert any("accepted-1" in error and "terminal" in error for error in errors)
    assert any("accepted-2" in error and "terminal" in error for error in errors)


def _accounting_row(run_id: str, tenant: str, replica: str, fault: str | None) -> dict:
    return {
        "request_id": f"request-{run_id}",
        "profile": "overload",
        "tenant_id": tenant,
        "deployment_ref": f"{tenant}-deployment-1",
        "replica": replica,
        "worker": "worker-1",
        "surface": "approvals",
        "fault": fault,
        "status_code": 202,
        "retry_after_seconds": None,
        "started_at_ms": 0.0,
        "finished_at_ms": 1.0,
        "latency_ms": 1.0,
        "queue_depth": 1,
        "cpu_percent": 1.0,
        "memory_bytes": 1,
        "lifecycle": [{"state": "accepted", "at_ms": 0.0, "run_id": run_id}],
    }

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.cloud.models import CloudUsageCounter
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.models import (
    DecisionSchedule,
    ProbabilisticDecisionSchedule,
    RandomizedRolloutAssignmentRecord,
    RandomizedRolloutRecord,
)
from zeroth.econ.plane.decisioning.service import (
    run_due_decision_schedules,
    run_due_probabilistic_decision_schedules,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _headers(monkeypatch, *, tenant_id: str, roles: str) -> dict[str, str]:
    monkeypatch.setattr(settings, "service_principal_tenant_id", tenant_id)
    monkeypatch.setattr(settings, "service_principal_roles", roles)
    token = mint_econ_service_token()
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def _client(engine, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    return TestClient(app)


def _seed_controls(engine) -> datetime:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    with Session(engine) as db:
        for tenant_id in ("tenant-a", "tenant-b"):
            suffix = tenant_id[-1]
            db.add(
                DecisionSchedule(
                    schedule_id=f"dsch-{suffix}",
                    tenant_id=tenant_id,
                    workflow="invoice-agent",
                    baseline_version="v1",
                    candidate_version="v2",
                    outcome_type="accepted",
                    policy_json={},
                    interval_minutes=60,
                    active=True,
                    next_run_at=now - timedelta(minutes=1),
                    last_run_at=None,
                    last_decision_id=None,
                    last_error=None,
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                    created_by="seed",
                )
            )
            db.add(
                ProbabilisticDecisionSchedule(
                    schedule_id=f"psch-{suffix}",
                    tenant_id=tenant_id,
                    evidence_source_json={
                        "workload": "invoice-agent",
                        "incumbent_model": "model-a",
                        "candidate_model": "model-b",
                    },
                    policy_json={"require_calibrated_forecast": False},
                    simulations=100,
                    seed=7,
                    interval_minutes=60,
                    active=True,
                    next_run_at=now - timedelta(minutes=1),
                    last_run_at=None,
                    last_decision_id=None,
                    last_error=None,
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                    created_by="seed",
                )
            )
            db.add(
                RandomizedRolloutRecord(
                    rollout_id=f"roll-{suffix}",
                    tenant_id=tenant_id,
                    decision_id="legacy-decision",
                    workload="invoice-agent",
                    incumbent_model="model-a",
                    candidate_model="model-b",
                    candidate_probability=0.5,
                    cohort_probabilities_json={},
                    assignment_salt="synthetic-salt",
                    minimum_per_arm=20,
                    active=True,
                    created_at=now - timedelta(days=1),
                    created_by="seed",
                )
            )
        db.add(
            RandomizedRolloutAssignmentRecord(
                assignment_id="rasn_"
                + hashlib.sha256(b"tenant-a:roll-a:existing-subject").hexdigest()[:24],
                tenant_id="tenant-a",
                rollout_id="roll-a",
                subject_id="existing-subject",
                cohort="default",
                arm="incumbent",
                assigned_model="model-a",
                assigned_at=now - timedelta(hours=1),
            )
        )
        db.add(
            CloudUsageCounter(
                tenant_id="tenant-a",
                period_start=now,
                meter="decision_scans",
                quantity=7,
                updated_at=now,
            )
        )
        db.commit()
    return now


def test_schedule_deactivation_is_idempotent_scoped_unmetered_and_not_due(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'controls.db'}")
    Base.metadata.create_all(engine)
    now = _seed_controls(engine)
    client = _client(engine, monkeypatch)
    admin = _headers(monkeypatch, tenant_id="tenant-a", roles="Admin")

    deterministic = client.post("/v1/decision-schedules/dsch-a/deactivate", headers=admin)
    assert deterministic.status_code == 200, deterministic.text
    assert deterministic.json()["active"] is False
    with Session(engine) as db:
        first_updated_at = db.get(DecisionSchedule, "dsch-a").updated_at
    repeated = client.post("/v1/decision-schedules/dsch-a/deactivate", headers=admin)
    assert repeated.status_code == 200
    assert repeated.json() == deterministic.json()

    analyst = _headers(monkeypatch, tenant_id="tenant-a", roles="Analyst")
    probabilistic = client.post(
        "/v1/probabilistic-decision-schedules/psch-a/deactivate",
        headers=analyst,
    )
    assert probabilistic.status_code == 200, probabilistic.text
    assert probabilistic.json()["active"] is False
    repeated_probabilistic = client.post(
        "/v1/probabilistic-decision-schedules/psch-a/deactivate",
        headers=analyst,
    )
    assert repeated_probabilistic.json() == probabilistic.json()

    deterministic_list = client.get("/v1/decision-schedules", headers=admin)
    probabilistic_list = client.get("/v1/probabilistic-decision-schedules", headers=admin)
    assert deterministic_list.json()[0]["active"] is False
    assert probabilistic_list.json()[0]["active"] is False

    cross_tenant = client.post("/v1/decision-schedules/dsch-b/deactivate", headers=admin)
    missing = client.post("/v1/decision-schedules/dsch-missing/deactivate", headers=admin)
    assert cross_tenant.status_code == missing.status_code == 404
    assert cross_tenant.json() == missing.json()
    probabilistic_cross_tenant = client.post(
        "/v1/probabilistic-decision-schedules/psch-b/deactivate",
        headers=analyst,
    )
    probabilistic_missing = client.post(
        "/v1/probabilistic-decision-schedules/psch-missing/deactivate",
        headers=analyst,
    )
    assert probabilistic_cross_tenant.status_code == probabilistic_missing.status_code == 404
    assert probabilistic_cross_tenant.json() == probabilistic_missing.json()

    viewer = _headers(monkeypatch, tenant_id="tenant-a", roles="Viewer")
    forbidden = client.post(
        "/v1/probabilistic-decision-schedules/psch-a/deactivate",
        headers=viewer,
    )
    assert forbidden.status_code == 403

    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        assert run_due_decision_schedules(db, now=now) == []
        assert run_due_probabilistic_decision_schedules(db, now=now) == []
    with Session(engine) as db:
        assert db.get(DecisionSchedule, "dsch-a").updated_at == first_updated_at
        assert db.get(CloudUsageCounter, ("tenant-a", now, "decision_scans")).quantity == 7


def test_rollout_stop_is_idempotent_scoped_and_rejects_only_new_assignments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'rollout-controls.db'}")
    Base.metadata.create_all(engine)
    _seed_controls(engine)
    client = _client(engine, monkeypatch)
    analyst = _headers(monkeypatch, tenant_id="tenant-a", roles="Analyst")

    stopped = client.post("/v1/randomized-rollouts/roll-a/stop", headers=analyst)
    repeated = client.post("/v1/randomized-rollouts/roll-a/stop", headers=analyst)
    assert stopped.status_code == repeated.status_code == 200
    assert stopped.json()["active"] is False
    assert repeated.json() == stopped.json()

    existing = client.post(
        "/v1/randomized-rollouts/roll-a/assignments",
        headers=analyst,
        json={"subject_id": "existing-subject"},
    )
    fresh = client.post(
        "/v1/randomized-rollouts/roll-a/assignments",
        headers=analyst,
        json={"subject_id": "new-subject"},
    )
    assert existing.status_code == 200, existing.text
    assert existing.json()["subject_id"] == "existing-subject"
    assert fresh.status_code == 409

    cross_tenant = client.post("/v1/randomized-rollouts/roll-b/stop", headers=analyst)
    missing = client.post("/v1/randomized-rollouts/roll-missing/stop", headers=analyst)
    assert cross_tenant.status_code == missing.status_code == 404
    assert cross_tenant.json() == missing.json()

    viewer = _headers(monkeypatch, tenant_id="tenant-a", roles="Viewer")
    assert client.post("/v1/randomized-rollouts/roll-a/stop", headers=viewer).status_code == 403
    with Session(engine) as db:
        assignments = list(db.scalars(select(RandomizedRolloutAssignmentRecord)))
        assert len(assignments) == 1

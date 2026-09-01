from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.models import DecisionSchedule
from zeroth.econ.plane.decisioning.schemas import DecisionScheduleCreate
from zeroth.econ.plane.decisioning.service import (
    create_decision_schedule,
    run_due_decision_schedules,
)
from zeroth.econ.plane.decisioning.workers import _run_due_decision_scans
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _seed(db: Session) -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    for version, cost in (("v1", "1"), ("v2", "0.6")):
        for index in range(10):
            run_id = f"{version}-{index}"
            timestamp = now + timedelta(seconds=index)
            db.add(
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id=f"{run_id}:1",
                    join_key=run_id,
                    timestamp=timestamp,
                    capability_id="invoice-agent",
                    implementation_id=version,
                    model_version="model-a",
                    token_cost_usd=Decimal(cost),
                    tool_cost_usd=Decimal("0"),
                    compute_cost_usd=Decimal("0"),
                    cost_measurement="measured",
                    usage_measurement="measured",
                    latency_ms=10,
                    compute_time_ms=0,
                    event_metadata={},
                )
            )
            db.add(
                OutcomeEvent(
                    tenant_id="tenant-a",
                    join_key=run_id,
                    execution_id="",
                    capability_id="invoice-agent",
                    implementation_id=version,
                    outcome_type="accepted",
                    outcome_payload_json={"accepted": index < 9},
                    outcome_value=str(index < 9).lower(),
                    occurred_at=timestamp,
                    ingested_at=timestamp,
                    outcome_timestamp=timestamp,
                    provenance="MEASURED",
                )
            )


def test_schedule_api_and_due_runner_retain_a_recurring_decision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'schedules.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        db.commit()

    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    created = client.post(
        "/v1/decision-schedules",
        headers=headers,
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "outcome_type": "accepted",
            "interval_minutes": 60,
        },
    )

    assert created.status_code == 200, created.text
    assert created.json()["active"] is True
    assert created.json()["last_decision_id"] is None
    schedule_id = created.json()["schedule_id"]

    with Session(engine) as raw:
        scoped = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        decisions = run_due_decision_schedules(
            scoped,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )

    assert len(decisions) == 1
    assert decisions[0].verdict == "pass"
    assert decisions[0].decision_id

    listed = client.get("/v1/decision-schedules", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["schedule_id"] == schedule_id
    assert listed.json()[0]["last_decision_id"] == decisions[0].decision_id
    assert datetime.fromisoformat(listed.json()[0]["next_run_at"]) > datetime.now(UTC)
    with Session(engine) as db:
        schedule = db.scalar(select(DecisionSchedule))
        assert schedule is not None
        assert schedule.last_error is None


def test_worker_discovers_due_tenants_internally(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as raw:
        _seed(raw)
        raw.commit()
    with Session(engine) as raw:
        create_decision_schedule(
            ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a")),
            DecisionScheduleCreate(
                workflow="invoice-agent",
                baseline_version="v1",
                candidate_version="v2",
                interval_minutes=60,
            ),
            created_by="test",
        )

    from zeroth.econ.plane.decisioning import workers

    monkeypatch.setattr(workers, "SessionLocal", lambda: Session(engine))

    processed = _run_due_decision_scans(now=datetime.now(UTC) + timedelta(seconds=1))

    assert processed == 1

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.models import EconomicDecisionRecord
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _seed_version(
    db: Session,
    *,
    tenant_id: str,
    version: str,
    cost: str,
    accepted: int,
) -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    for index in range(10):
        run_id = f"{version}-{index}"
        timestamp = now + timedelta(seconds=index)
        db.add(
            ExecutionEvent(
                tenant_id=tenant_id,
                execution_id=f"{run_id}:generate:1",
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
                event_metadata={"step": "generate"},
            )
        )
        db.add(
            OutcomeEvent(
                tenant_id=tenant_id,
                join_key=run_id,
                execution_id="",
                capability_id="invoice-agent",
                implementation_id=version,
                outcome_type="accepted",
                outcome_payload_json={"accepted": index < accepted},
                outcome_value=str(index < accepted).lower(),
                occurred_at=timestamp,
                ingested_at=timestamp,
                outcome_timestamp=timestamp,
                provenance="MEASURED",
            )
        )


def test_compare_route_reads_only_the_authenticated_tenant(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'decisions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed_version(db, tenant_id="tenant-a", version="v1", cost="1", accepted=9)
        _seed_version(db, tenant_id="tenant-a", version="v2", cost="0.6", accepted=9)
        _seed_version(db, tenant_id="tenant-b", version="v2", cost="99", accepted=1)
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
    response = client.post(
        "/v1/decisions/compare",
        headers=headers,
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "outcome_type": "accepted",
            "policy": {
                "min_runs": 10,
                "min_outcome_coverage": 1,
                "min_success_rate": 0.85,
                "max_success_rate_drop": 0.02,
                "max_cost_per_outcome_increase": 0,
            },
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verdict"] == "pass"
    assert payload["recommended_action"] == "approve"
    assert payload["baseline"]["runs"] == 10
    assert payload["candidate"]["runs"] == 10
    assert payload["cost_per_outcome_change"] == -0.4
    assert payload["decision_id"].startswith("dec_")
    assert payload["evaluated_at"]

    repeated = client.post(
        "/v1/decisions/compare",
        headers=headers,
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "outcome_type": "accepted",
            "policy": {
                "min_runs": 10,
                "min_outcome_coverage": 1,
                "min_success_rate": 0.85,
                "max_success_rate_drop": 0.02,
                "max_cost_per_outcome_increase": 0,
            },
        },
    )
    history = client.get("/v1/decisions", headers=headers)

    assert repeated.status_code == 200
    assert repeated.json()["decision_id"] == payload["decision_id"]
    assert history.status_code == 200
    assert [item["decision_id"] for item in history.json()] == [payload["decision_id"]]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(EconomicDecisionRecord)) == 1


def test_compare_route_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    response = TestClient(app).post(
        "/v1/decisions/compare",
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
        },
    )

    assert response.status_code in {401, 403}

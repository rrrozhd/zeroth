from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _cloud_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    return app


def test_sdk_execution_and_outcome_routes_persist_joinable_versioned_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sdk-cloud.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    timestamp = datetime(2026, 8, 31, tzinfo=UTC).isoformat()
    client = TestClient(_cloud_app(engine))

    execution = {
        "workflow": "invoice-agent",
        "workflow_version": "v7",
        "run_id": "run-1",
        "step": "extract",
        "attempt": 2,
        "recorded_at": timestamp,
        "model_version": "gpt-test",
        "cost_usd": "0.031",
        "cost_measurement": "measured",
        "latency_ms": 420,
        "subject_id": "customer-4",
        "dimensions": {"plan": "pro"},
        "metadata": {"region": "us"},
    }
    first_execution = client.post("/v1/executions", headers=headers, json=execution)
    duplicate_execution = client.post("/v1/executions", headers=headers, json=execution)

    outcome = {
        "workflow": "invoice-agent",
        "workflow_version": "v7",
        "run_id": "run-1",
        "accepted": True,
        "outcome_type": "accepted",
        "occurred_at": timestamp,
        "provenance": "measured",
        "value_usd": "1.20",
        "score": 0.99,
        "subject_id": "customer-4",
        "dimensions": {"plan": "pro"},
        "metadata": {"source": "billing"},
    }
    first_outcome = client.post("/v1/outcomes", headers=headers, json=outcome)
    duplicate_outcome = client.post("/v1/outcomes", headers=headers, json=outcome)

    assert first_execution.status_code == 200, first_execution.text
    assert first_execution.json()["status"] == "inserted"
    assert duplicate_execution.json()["status"] == "duplicate"
    assert first_outcome.status_code == 200, first_outcome.text
    assert first_outcome.json()["status"] == "inserted"
    assert duplicate_outcome.json()["status"] == "duplicate"

    with Session(engine) as db:
        stored_execution = db.scalars(select(ExecutionEvent)).one()
        stored_outcome = db.scalars(select(OutcomeEvent)).one()
    assert stored_execution.tenant_id == "tenant-a"
    assert stored_execution.join_key == "run-1"
    assert stored_execution.capability_id == "invoice-agent"
    assert stored_execution.implementation_id == "v7"
    assert stored_execution.event_metadata == {
        "attempt": 2,
        "dimensions": {"plan": "pro"},
        "region": "us",
        "step": "extract",
        "subject_id": "customer-4",
        "tenant_id": "tenant-a",
    }
    assert stored_outcome.join_key == "run-1"
    assert stored_outcome.implementation_id == "v7"
    assert stored_outcome.outcome_payload_json["accepted"] is True
    assert stored_outcome.outcome_payload_json["dimensions"] == {"plan": "pro"}


def test_sdk_cloud_routes_require_authentication() -> None:
    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")

    response = TestClient(app).post(
        "/v1/executions",
        json={
            "workflow": "invoice-agent",
            "run_id": "run-1",
            "step": "extract",
        },
    )

    assert response.status_code in {401, 403}

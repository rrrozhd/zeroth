from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_cloud_api import _cloud_app
from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.cloud import api
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation.models import OutcomeEvent


@pytest.fixture
def metered_outcomes(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outcomes.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            CloudSubscription(
                tenant_id="tenant-a",
                plan="trial",
                status="trialing",
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=13),
                updated_at=now,
            )
        )
        db.commit()
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token
    with TestClient(_cloud_app(engine), raise_server_exceptions=False) as client:
        seeded = client.post(
            "/v1/executions",
            headers={"Authorization": f"Bearer {token}"},
            json=dict(
                workflow="invoice-agent", workflow_version="v1", run_id="run-1", step="extract"
            ),
        )
        assert seeded.status_code == 200, seeded.text
        monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
        yield engine, client, {"Authorization": f"Bearer {token}"}
    engine.dispose()


def _outcome(outcome_type="accepted"):
    return dict(
        workflow="invoice-agent",
        workflow_version="v1",
        run_id="run-1",
        accepted=True,
        outcome_type=outcome_type,
        occurred_at="2026-09-01T00:00:00Z",
    )


def _retained_counts(engine):
    with Session(engine) as db:
        quantity = db.scalar(select(func.sum(CloudUsageCounter.quantity))) or 0
        outcomes = db.scalar(select(func.count()).select_from(OutcomeEvent))
        return quantity, outcomes


@pytest.mark.parametrize("length,expected_status", [(64, 200), (65, 422)])
def test_outcome_type_bound_is_enforced_before_charging(metered_outcomes, length, expected_status):
    engine, client, headers = metered_outcomes
    response = client.post("/v1/outcomes", headers=headers, json=_outcome("x" * length))
    assert response.status_code == expected_status, response.text
    expected_count = int(expected_status == 200)
    assert _retained_counts(engine) == (expected_count, expected_count)


def test_adapter_validation_failure_refunds_reservation(metered_outcomes, monkeypatch):
    engine, client, headers = metered_outcomes
    adapter = api._CloudOutcomeCreate

    def rejected_adapter(**values):
        return adapter(**{**values, "outcome_type": ""})

    monkeypatch.setattr(api, "_CloudOutcomeCreate", rejected_adapter)
    response = client.post("/v1/outcomes", headers=headers, json=_outcome())
    assert response.status_code == 422, response.text
    assert _retained_counts(engine) == (0, 0)
    monkeypatch.setattr(api, "_CloudOutcomeCreate", adapter)
    accepted = client.post("/v1/outcomes", headers=headers, json=_outcome())
    duplicate = client.post("/v1/outcomes", headers=headers, json=_outcome())
    assert accepted.status_code == duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert _retained_counts(engine) == (1, 1)

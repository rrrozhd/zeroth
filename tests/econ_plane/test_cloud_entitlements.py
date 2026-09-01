from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.cloud.entitlements import (
    PLAN_CATALOG,
    EntitlementError,
    PlanLimits,
    reserve_usage,
)
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def test_cloud_event_quota_is_enforced_without_charging_duplicate_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'entitlements.db'}")
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
                external_customer_id=None,
                external_subscription_id=None,
                updated_at=now,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(
            event_limit=1,
            decision_scan_limit=1,
            backtest_limit=1,
            backtest_call_limit=1,
            schedule_limit=1,
            minimum_schedule_interval_minutes=1440,
        ),
    )
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    event = {
        "workflow": "invoice-agent",
        "workflow_version": "v1",
        "run_id": "run-1",
        "step": "extract",
        "recorded_at": datetime(2026, 8, 31, tzinfo=UTC).isoformat(),
        "cost_usd": "0.02",
    }

    first = client.post("/v1/executions", headers=headers, json=event)
    duplicate = client.post("/v1/executions", headers=headers, json=event)
    over_limit = client.post(
        "/v1/executions",
        headers=headers,
        json={**event, "run_id": "run-2"},
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert over_limit.status_code == 402
    assert over_limit.json()["detail"] == "trial event limit reached"


def test_self_hosted_mode_does_not_require_a_subscription(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'self-hosted.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None

    response = TestClient(app).post(
        "/v1/executions",
        headers={"Authorization": f"Bearer {token}"},
        json={"workflow": "invoice-agent", "run_id": "run-1", "step": "extract"},
    )

    assert response.status_code == 200


def test_paddle_trialing_solo_subscription_keeps_trial_limits(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'paddle-trial.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(1, 1, 1, 1, 1, 1440),
    )
    with Session(engine) as session:
        session.add(
            CloudSubscription(
                tenant_id="tenant-a",
                plan="solo",
                status="trialing",
                period_start=now,
                period_end=now + timedelta(days=14),
                external_customer_id="ctm_01",
                external_subscription_id="sub_01",
                billing_provider="paddle",
                updated_at=now,
            )
        )
        session.commit()
        scoped = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))

        assert reserve_usage(scoped, "backtests") is True
        with pytest.raises(EntitlementError, match="trial backtest limit reached"):
            reserve_usage(scoped, "backtests")


def test_trial_limits_decision_scans_and_schedule_frequency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'decision-limits.db'}")
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
                external_customer_id=None,
                external_subscription_id=None,
                updated_at=now,
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    too_frequent = client.post(
        "/v1/decision-schedules",
        headers=headers,
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "interval_minutes": 60,
        },
    )
    schedule = client.post(
        "/v1/decision-schedules",
        headers=headers,
        json={
            "workflow": "invoice-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "interval_minutes": 1440,
        },
    )
    schedule_over_limit = client.post(
        "/v1/decision-schedules",
        headers=headers,
        json={
            "workflow": "another-agent",
            "baseline_version": "v1",
            "candidate_version": "v2",
            "interval_minutes": 1440,
        },
    )
    request = {
        "workflow": "invoice-agent",
        "baseline_version": "v1",
        "candidate_version": "v2",
    }
    first_scan = client.post("/v1/decisions/compare", headers=headers, json=request)
    second_scan = client.post("/v1/decisions/compare", headers=headers, json=request)

    assert too_frequent.status_code == 402
    assert schedule.status_code == 200
    assert schedule_over_limit.status_code == 402
    assert first_scan.status_code == 200
    assert first_scan.json()["verdict"] == "abstain"
    assert second_scan.status_code == 402

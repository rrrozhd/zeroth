from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.backtesting.api import get_backtest_executor, router
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.backtesting.schemas import BacktestComputation
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.cloud.entitlements import PLAN_CATALOG, PlanLimits
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _payload):  # type: ignore[no-untyped-def]
        self.calls += 1
        return BacktestComputation(
            incumbent_success_rate=0.96,
            candidate_success_rate=0.96,
            candidate_error_rate=0.0,
            savings_pct=61.0,
            provider_calls=20,
        )


def _payload() -> dict[str, object]:
    return {
        "workflow": "invoice-agent",
        "baseline_version": "v7",
        "node_id": "extract",
        "incumbent_model": "openai/gpt-5-mini",
        "instruction": "Extract invoice fields.",
        "candidate": {"model": "openai/gpt-5-nano"},
        "cases": [
            {
                "id": f"invoice-{index}",
                "input": {"text": f"invoice {index}"},
                "expected": {"total": str(index)},
            }
            for index in range(5)
        ],
        "constraints": {"min_success_rate": 0.95},
    }


def test_backtest_is_retained_without_raw_cases_and_duplicate_is_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backtests.db'}")
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
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    executor = _Executor()
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(
            event_limit=1,
            decision_scan_limit=1,
            backtest_limit=1,
            backtest_call_limit=20,
            schedule_limit=1,
            minimum_schedule_interval_minutes=1440,
        ),
    )
    token = mint_econ_service_token()
    assert token is not None
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/v1/backtests", headers=headers, json=_payload())
    duplicate = client.post("/v1/backtests", headers=headers, json=_payload())
    history = client.get("/v1/backtests", headers=headers)

    assert first.status_code == 200, first.text
    assert first.json()["verdict"] == "pass"
    assert first.json()["recommended_action"] == "approve_candidate"
    assert first.json()["provider_call_credits"] == 20
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert history.json() == [first.json()]
    assert executor.calls == 1
    with Session(engine) as db:
        record = db.scalar(select(EconomicBacktestRecord))
        assert record is not None
        serialized = str(record.report_json)
        assert "invoice 1" not in serialized
        assert "Extract invoice fields" not in serialized
        usage = db.get(CloudUsageCounter, ("tenant-a", record.period_start, "backtest_calls"))
        assert usage is not None
        assert usage.quantity == 20


def test_backtest_count_limit_is_independent_from_provider_call_credits(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backtest-count.db'}")
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
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    executor = _Executor()
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(
            event_limit=1,
            decision_scan_limit=1,
            backtest_limit=1,
            backtest_call_limit=100,
            schedule_limit=1,
            minimum_schedule_interval_minutes=1440,
        ),
    )
    token = mint_econ_service_token()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/v1/backtests", headers=headers, json=_payload())
    changed = _payload()
    changed["candidate"] = {"model": "openai/gpt-5-mini-2026-09"}
    second = client.post("/v1/backtests", headers=headers, json=changed)

    assert first.status_code == 200
    assert second.status_code == 402
    assert second.json()["detail"] == "trial backtest limit reached"
    assert executor.calls == 1


def test_backtest_meter_reservation_is_atomic_when_call_credits_are_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'atomic-backtest-quota.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    period_start = now - timedelta(days=1)
    with Session(engine) as db:
        db.add(
            CloudSubscription(
                tenant_id="tenant-a",
                plan="trial",
                status="trialing",
                period_start=period_start,
                period_end=now + timedelta(days=13),
                external_customer_id=None,
                external_subscription_id=None,
                updated_at=now,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    executor = _Executor()
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setitem(
        PLAN_CATALOG,
        "trial",
        PlanLimits(
            event_limit=1,
            decision_scan_limit=1,
            backtest_limit=1,
            backtest_call_limit=19,
            schedule_limit=1,
            minimum_schedule_interval_minutes=1440,
        ),
    )
    token = mint_econ_service_token()

    response = TestClient(app).post(
        "/v1/backtests",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(),
    )

    assert response.status_code == 402
    assert response.json()["detail"] == "trial backtest call limit reached"
    assert executor.calls == 0
    with Session(engine) as db:
        count = db.get(CloudUsageCounter, ("tenant-a", period_start, "backtests"))
        assert count is None


def test_backtest_abstains_without_executable_evidence(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'abstain.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    executor = _Executor()
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None

    response = TestClient(app).post(
        "/v1/backtests",
        headers={"Authorization": f"Bearer {token}"},
        json={"workflow": "invoice-agent", "candidate": {"model": "gpt-5-mini"}, "constraints": {}},
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "abstain"
    assert "cases" in response.json()["reasons"]
    assert executor.calls == 0


def test_backtest_abstains_for_constraints_the_node_replay_cannot_prove(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'unsupported.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    executor = _Executor()
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: executor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    payload = _payload()
    payload["constraints"] = {"max_critical_error_rate": 0.01}

    response = TestClient(app).post(
        "/v1/backtests", headers={"Authorization": f"Bearer {token}"}, json=payload
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "abstain"
    assert "critical-error evidence" in " ".join(response.json()["reasons"])
    assert executor.calls == 0

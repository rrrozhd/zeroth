"""Real SDK contracts dispatched through the authenticated plane API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.backtesting.api import get_backtest_executor, router as backtesting_router
from zeroth.econ.plane.backtesting.schemas import BacktestComputation
from zeroth.econ.plane.cloud.api import router as cloud_router
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


class _BacktestExecutor:
    async def execute(self, _payload):  # type: ignore[no-untyped-def]
        return BacktestComputation(
            incumbent_success_rate=1,
            candidate_success_rate=1,
            candidate_error_rate=0,
            savings_pct=50,
            provider_calls=20,
        )


def test_sdk_events_produce_a_hosted_economic_decision(tmp_path: Path, monkeypatch) -> None:
    from zeroth.protocol import ExecutionEvent, OutcomeEvent, VersionComparisonRequest
    from zeroth.sdk import ZerothClient

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sdk-e2e.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(cloud_router, prefix="/v1")
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    api = TestClient(app)

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = api.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    sdk = ZerothClient(
        api_key=token,
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(transport=httpx.MockTransport(dispatch)),
    )
    now = datetime(2026, 8, 31, tzinfo=UTC)
    for version, cost in (("v1", Decimal("1")), ("v2", Decimal("0.6"))):
        for index in range(10):
            run_id = f"{version}-{index}"
            timestamp = now + timedelta(seconds=index)
            sdk.record_execution(
                ExecutionEvent(
                    workflow="invoice-agent",
                    workflow_version=version,
                    run_id=run_id,
                    step="generate",
                    recorded_at=timestamp,
                    cost_usd=cost,
                )
            )
            sdk.record_outcome(
                OutcomeEvent(
                    workflow="invoice-agent",
                    workflow_version=version,
                    run_id=run_id,
                    accepted=index < 9,
                    occurred_at=timestamp,
                )
            )

    decision = sdk.compare_versions(
        VersionComparisonRequest(
            workflow="invoice-agent",
            baseline_version="v1",
            candidate_version="v2",
        )
    )

    assert decision["verdict"] == "pass"
    assert decision["recommended_action"] == "approve"
    assert decision["cost_per_outcome_change"] == -0.4


def test_sdk_submits_and_reads_a_real_hosted_backtest_route(tmp_path: Path, monkeypatch) -> None:
    from zeroth.protocol import BacktestCase, BacktestRequest, EconomicConstraints
    from zeroth.sdk import ZerothClient

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sdk-backtest.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(backtesting_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = _BacktestExecutor
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    api = TestClient(app)

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = api.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    sdk = ZerothClient(
        api_key=token,
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(transport=httpx.MockTransport(dispatch)),
    )
    result = sdk.create_backtest(
        BacktestRequest(
            workflow="invoice-agent",
            baseline_version="v7",
            node_id="extract",
            incumbent_model="openai/incumbent",
            instruction="Extract invoice fields.",
            candidate={"model": "openai/candidate"},
            cases=[
                BacktestCase(
                    id=str(index), input={"text": str(index)}, expected={"total": str(index)}
                )
                for index in range(5)
            ],
            constraints=EconomicConstraints(min_success_rate=0.95),
        )
    )

    assert result["verdict"] == "pass"
    assert sdk.list_backtests() == [result]


def test_sdk_submits_and_reads_a_probabilistic_model_migration_decision(
    tmp_path: Path, monkeypatch
) -> None:
    from zeroth.protocol import (
        MigrationEvidence,
        MigrationObservation,
        MigrationRiskPolicy,
        ProbabilisticMigrationRequest,
        ForecastCalibrationObservation,
    )
    from zeroth.sdk import ZerothClient

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sdk-probabilistic.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    api = TestClient(app)

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = api.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    sdk = ZerothClient(
        api_key=token,
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(transport=httpx.MockTransport(dispatch)),
    )
    incumbent = [
        MigrationObservation(
            case_id=f"case-{index}",
            cost_usd=Decimal("1"),
            latency_ms=800,
            accepted=True,
            critical_error=index == 0,
            source="production",
        )
        for index in range(100)
    ]
    candidate = [
        MigrationObservation(
            case_id=f"case-{index}",
            cost_usd=Decimal("0.5"),
            latency_ms=700,
            accepted=True,
            critical_error=index == 0,
            source="replay",
        )
        for index in range(100)
    ]
    request = ProbabilisticMigrationRequest(
        evidence=MigrationEvidence(
            workload="invoice-agent",
            incumbent_model="model-a",
            candidate_model="model-b",
            incumbent=incumbent,
            candidate=candidate,
            period_request_counts=[100],
            demand_horizon="month",
        ),
        policy=MigrationRiskPolicy(
            candidate_shares=[1.0],
            max_quality_drop=0.02,
            max_p95_latency_ms=1_000,
            max_critical_error_rate=0.05,
            max_constraint_breach_probability=0.1,
            max_cvar_loss_usd=Decimal("0"),
        ),
        calibration_observations=[
            ForecastCalibrationObservation(
                forecast_id=f"{metric}-{index}",
                metric=metric,
                predicted_mean=100,
                predicted_low=90,
                predicted_high=110,
                observed=100,
                observed_at=datetime(2026, index + 1, 1, tzinfo=UTC),
            )
            for metric in (
                "monthly_cost_usd",
                "success_rate",
                "p95_latency_ms",
                "critical_error_rate",
            )
            for index in range(6)
        ],
        simulations=400,
        seed=17,
    )

    result = sdk.create_model_migration_decision(request)

    assert result["recommended_action"] == "collect_evidence"
    assert "experimental_predictive_reliability_unapproved" in result["reason_codes"]
    assert result["evidence_lineage"]["predictive_reliability"] == "unapproved"
    assert Decimal(result["actions"][0]["cvar_loss_usd"]) < 0
    assert sdk.list_model_migration_decisions() == [result]

"""Retained aggregate backtests are missing evidence, not refresh failures."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.backtesting.schemas import BacktestComputation, BacktestCreate
from zeroth.econ.plane.backtesting.service import decide, retain_backtest
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router
from zeroth.econ.plane.decisioning.evidence import harvest_migration_evidence
from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
from zeroth.econ.plane.decisioning.schemas import (
    MigrationEvidenceSource,
    ProbabilisticDecisionScheduleCreate,
)
from zeroth.econ.plane.decisioning.service import (
    create_probabilistic_decision_schedule,
    list_probabilistic_decision_schedules,
    run_due_probabilistic_decision_schedules,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


SOURCE = MigrationEvidenceSource(
    workload="invoice-agent", incumbent_model="model-a", candidate_model="model-b"
)


@pytest.fixture
def retained_aggregate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty-backtest.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC) - timedelta(seconds=1)
    payload = BacktestCreate(
        workflow=SOURCE.workload,
        incumbent_model=SOURCE.incumbent_model,
        candidate={"model": SOURCE.candidate_model},
        constraints={},
    )
    computation = BacktestComputation(
        incumbent_success_rate=1, candidate_success_rate=1, savings_pct=80
    )
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        report = retain_backtest(
            db,
            decide(payload, computation, digest="aggregate-only", evaluated_at=now),
            evaluated_by="test",
        )
    yield engine, now, report.backtest_id
    engine.dispose()


@pytest.mark.parametrize(
    "missing", [None, "incumbent_observations", "candidate_observations", "period_request_counts"]
)
@pytest.mark.parametrize("omit", [False, True])
def test_empty_or_missing_backtest_arrays_are_evidence_gaps(retained_aggregate, missing, omit):
    engine, now, backtest_id = retained_aggregate
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        record = db.get(EconomicBacktestRecord, backtest_id)
        if missing is not None:
            row = dict(case_id="case-1", cost_usd="1", latency_ms=500, accepted=True, source="test")
            artifact = {
                **record.report_json,
                "incumbent_observations": [row],
                "candidate_observations": [row],
                "period_request_counts": [100],
            }
            if omit:
                artifact.pop(missing)
            else:
                artifact[missing] = []
            record.report_json = artifact
            db.commit()
        result = harvest_migration_evidence(db, SOURCE, now=now)
    assert result.evidence is None
    assert result.gaps == ["no_paired_case_level_evidence"]
    assert result.lineage.paired_cases == 0


def test_nonempty_malformed_artifact_still_fails_validation(retained_aggregate):
    engine, now, backtest_id = retained_aggregate
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        record = db.get(EconomicBacktestRecord, backtest_id)
        record.report_json = {
            **record.report_json,
            "incumbent_observations": [{}],
            "candidate_observations": [{}],
            "period_request_counts": [100],
        }
        db.commit()
        with pytest.raises(ValidationError):
            harvest_migration_evidence(db, SOURCE, now=now)


def test_refresh_api_retains_missing_evidence_abstention(retained_aggregate):
    engine, _, _ = retained_aggregate
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as raw:
            yield ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    token = mint_econ_service_token()
    assert token is not None
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/decisions/model-migration/refresh",
            headers={"Authorization": f"Bearer {token}"},
            json={"evidence_source": SOURCE.model_dump(), "policy": {}, "simulations": 100},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["verdict"] == "abstain"
        assert result["recommended_action"] == "collect_evidence"
        assert result["recommended_candidate_share"] == 0
        assert result["reason_codes"] == ["no_paired_case_level_evidence"]
        assert result["actions"] == []
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        record = db.get(ProbabilisticMigrationDecisionRecord, result["decision_id"])
        assert record.evidence_json == {"gaps": ["no_paired_case_level_evidence"]}
        assert record.evidence_lineage_json["paired_cases"] == 0


def test_schedule_retains_abstention_and_completes_without_error(retained_aggregate):
    engine, now, _ = retained_aggregate
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        created = create_probabilistic_decision_schedule(
            db,
            ProbabilisticDecisionScheduleCreate(evidence_source=SOURCE, policy={}, simulations=100),
            created_by="test",
            now=now,
        )
        completed = run_due_probabilistic_decision_schedules(db, now=now)
        assert len(completed) == 1
        decision = completed[0]
        assert decision.verdict == "abstain"
        assert decision.reason_codes == ["no_paired_case_level_evidence"]
        schedule = list_probabilistic_decision_schedules(db)[0]
        assert schedule.schedule_id == created.schedule_id
        assert schedule.last_error is None
        assert schedule.last_decision_id == decision.decision_id
        assert schedule.last_run_at == now and schedule.next_run_at > now
        assert run_due_probabilistic_decision_schedules(db, now=now) == []
        records = list(db.scalars(select(ProbabilisticMigrationDecisionRecord)))
        assert len(records) == 1
        assert records[0].evidence_json == {"gaps": ["no_paired_case_level_evidence"]}

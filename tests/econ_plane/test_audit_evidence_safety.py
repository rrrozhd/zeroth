"""Actual refresh/store/schedule regressions for the first package audit slice."""

from datetime import UTC, datetime, timedelta
import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router
from zeroth.econ.plane.decisioning.evidence import harvest_migration_evidence
from zeroth.econ.plane.decisioning.models import (
    ForecastCalibrationRecord,
    ProbabilisticMigrationDecisionRecord,
    RandomizedRolloutRecord,
    RandomizedRolloutAssignmentRecord,
    RandomizedRolloutVerificationRecord,
)
from zeroth.econ.plane.decisioning.schemas import (
    MigrationEvidenceSource,
    ProbabilisticDecisionScheduleCreate,
)
from zeroth.econ.plane.decisioning import service
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext
from zeroth.econ.rollout_verification import RolloutAssignment, RolloutVerification
from tests.econ_plane.test_probabilistic_evidence_harvesting import _seed_run

NOW = datetime.now(UTC) - timedelta(seconds=1)
SOURCE = MigrationEvidenceSource(
    workload="invoice-agent", incumbent_model="model-a", candidate_model="model-b"
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    value = create_engine(f"sqlite+pysqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def _db(raw):
    return ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))


def _calibration(index, when, *, observed=1.0, tenant="tenant-a", **changes):
    values = dict(
        tenant_id=tenant,
        forecast_id=f"f-{index}",
        verification_id=f"v-{index}",
        workload=SOURCE.workload,
        incumbent_model=SOURCE.incumbent_model,
        candidate_model=SOURCE.candidate_model,
        metric="monthly_cost_usd",
        predicted_mean=1.0,
        predicted_low=0.0,
        predicted_high=2.0,
        observed=observed,
        observed_at=when,
    )
    values.update(changes)
    return ForecastCalibrationRecord(**values)


def _pairs(raw, *, mismatch=False):
    for i in range(5):
        for model in ("model-a", "model-b"):
            _seed_run(
                raw,
                tenant="tenant-a",
                case=f"case-{i}",
                model=model,
                cost="1",
                accepted=True,
                cohort="different" if mismatch and model == "model-b" else "default",
                when=NOW,
            )
    raw.commit()


def _refresh(engine):
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped():
        with Session(engine) as raw:
            yield _db(raw)

    app.dependency_overrides[get_cloud_scoped_db] = scoped
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/decisions/model-migration/refresh",
            headers={"Authorization": f"Bearer {mint_econ_service_token()}"},
            json={"evidence_source": SOURCE.model_dump(), "policy": {}, "simulations": 100},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _scheduled(engine):
    with Session(engine) as raw:
        db = _db(raw)
        service.create_probabilistic_decision_schedule(
            db,
            ProbabilisticDecisionScheduleCreate(evidence_source=SOURCE, policy={}, simulations=100),
            created_by="test",
            now=NOW,
        )
        decisions = service.run_due_probabilistic_decision_schedules(db, now=NOW)
        assert len(decisions) == 1
        schedule = service.list_probabilistic_decision_schedules(db)[0]
        assert schedule.last_error is None
        assert schedule.last_decision_id == decisions[0].decision_id
        assert db.get(ProbabilisticMigrationDecisionRecord, decisions[0].decision_id)
        return decisions[0].model_dump(mode="json")


def test_latest_calibration_rows_are_selected_before_validation(engine):
    with Session(engine) as raw:
        for i in range(206):
            raw.add(
                _calibration(i, NOW - timedelta(days=206 - i), observed=100.0 if i >= 200 else 1.0)
            )
        raw.add(_calibration(999, NOW, tenant="foreign", observed=999.0))
        raw.commit()
        selected = service._calibration_observations_for_source(_db(raw), SOURCE)
    assert len(selected) == 200
    assert {row.forecast_id for row in selected} == {f"f-{i}" for i in range(6, 206)}
    assert [row.observed_at for row in selected] == sorted(row.observed_at for row in selected)
    assert sum(row.observed == 100.0 for row in selected) == 6


@pytest.mark.parametrize("with_pairs", [False, True])
@pytest.mark.parametrize(
    "changes",
    [{"predicted_low": 3.0}, {"predicted_high": float("inf")}, {"observed": float("inf")}],
)
def test_poisoned_selection_is_quarantined_without_certifying_remainder(
    engine, with_pairs, changes
):
    with Session(engine) as raw:
        if with_pairs:
            _pairs(raw)
        for i in range(205):
            raw.add(_calibration(i, NOW - timedelta(days=206 - i)))
        bad = _calibration(300, NOW, **changes)
        raw.add(bad)
        raw.commit()
        bad_id = bad.id
    for result in (_refresh(engine), _scheduled(engine)):
        assert result["verdict"] == "abstain"
        assert result["actions"] == []
        assert result["forecast_readiness"]["calibration_state"] == "unknown"
        assert "invalid_retained_calibration" in result["reason_codes"]
        quarantine = result["evidence_lineage"]["calibration_evidence"]["quarantined"]
        assert {row["row_id"] for row in quarantine} == {bad_id}
    with Session(engine) as raw:
        # Quarantine is diagnostic, never deletion or rewriting of history.
        assert _db(raw).get(ForecastCalibrationRecord, bad_id) is not None


def test_conflicting_forecast_identity_is_quarantined(engine):
    with Session(engine) as raw:
        raw.add(_calibration(1, NOW))
        raw.add(_calibration(2, NOW, forecast_id="f-1", observed=99.0))
        raw.commit()
    result = _refresh(engine)
    assert result["forecast_readiness"]["calibration_state"] == "unknown"
    assert "conflicting_retained_calibration" in result["reason_codes"]


def test_mean_outside_interval_is_not_malformed(engine):
    with Session(engine) as raw:
        raw.add(_calibration(1, NOW, predicted_mean=100.0, predicted_low=1.0, predicted_high=2.0))
        raw.commit()
        selected = service._calibration_observations_for_source(_db(raw), SOURCE)
    assert len(selected) == 1 and selected[0].predicted_mean == 100.0


def test_cohort_mismatch_is_an_explicit_retained_gap(engine):
    with Session(engine) as raw:
        _pairs(raw, mismatch=True)
    for result in (_refresh(engine), _scheduled(engine)):
        assert result["verdict"] == "abstain"
        assert result["reason_codes"] == ["paired_cohort_mismatch"]
        assert result["actions"] == []


def test_daily_counts_remain_unknown_horizon(engine):
    with Session(engine) as raw:
        _pairs(raw)
        evidence = harvest_migration_evidence(_db(raw), SOURCE, now=NOW).evidence
        assert evidence.demand_horizon == "unknown"
    result = _refresh(engine)
    assert result["verdict"] == "abstain"
    assert "demand_horizon_unknown" in result["reason_codes"]


def test_retained_backtest_cohort_mismatch_is_an_explicit_gap(engine):
    row = dict(case_id="case", cost_usd="1", latency_ms=500, accepted=True, source="test")
    with Session(engine) as raw:
        raw.add(
            EconomicBacktestRecord(
                backtest_id="mismatched-backtest",
                tenant_id="tenant-a",
                request_digest="test",
                workflow="invoice-agent",
                baseline_version="v1",
                node_id="extract",
                incumbent_model="model-a",
                candidate_model="model-b",
                verdict="pass",
                provider_call_credits=0,
                report_json={
                    "incumbent_observations": [{**row, "cohort": "a"}],
                    "candidate_observations": [{**row, "cohort": "b"}],
                    "period_request_counts": [100],
                },
                period_start=NOW,
                evaluated_at=NOW,
                evaluated_by="test",
            )
        )
        raw.commit()
    for result in (_refresh(engine), _scheduled(engine)):
        assert result["verdict"] == "abstain"
        assert result["reason_codes"] == ["paired_cohort_mismatch"]


@pytest.fixture
def legacy_heterogeneous(engine):
    with Session(engine) as raw:
        db = _db(raw)
        rollout = RandomizedRolloutRecord(
            rollout_id="legacy-heterogeneous",
            tenant_id="tenant-a",
            decision_id="legacy",
            workload=SOURCE.workload,
            incumbent_model="model-a",
            candidate_model="model-b",
            candidate_probability=0.5,
            cohort_probabilities_json={"high": 0.9, "low": 0.1},
            assignment_salt="legacy-synthetic-salt",
            minimum_per_arm=1,
            active=True,
            created_at=NOW - timedelta(days=1),
            created_by="test",
        )
        db.add(rollout)
        for i in range(6):
            model = "model-a" if i < 3 else "model-b"
            db.add(
                RandomizedRolloutAssignmentRecord(
                    assignment_id=f"assign-{i}",
                    tenant_id="tenant-a",
                    rollout_id=rollout.rollout_id,
                    subject_id=f"case-{i}",
                    cohort="default",
                    arm="incumbent" if i < 3 else "candidate",
                    assigned_model=model,
                    assigned_at=NOW - timedelta(hours=1),
                )
            )
            _seed_run(
                raw,
                tenant="tenant-a",
                case=f"case-{i}",
                model=model,
                cost="1",
                accepted=True,
                cohort="default",
                when=NOW,
            )
        db.commit()
        assignments = list(db.scalars(select(RandomizedRolloutAssignmentRecord)))
        domain = [
            RolloutAssignment(
                subject_id=r.subject_id,
                arm=r.arm,
                assigned_model=r.assigned_model,
                assigned_at=r.assigned_at.replace(tzinfo=UTC),
            )
            for r in assignments
        ]
        observations = service._rollout_observations_from_store(
            db, rollout, assignments, outcome_type="accepted"
        )
        legacy_digest = hashlib.sha256(
            json.dumps(
                dict(
                    rollout_id=rollout.rollout_id,
                    outcome_type="accepted",
                    bootstrap_samples=100,
                    seed=7,
                    assignments=[r.model_dump(mode="json") for r in domain],
                    observations=[r.model_dump(mode="json") for r in observations],
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        legacy_id = "rver_" + legacy_digest[:24]
        report = RolloutVerification(
            rollout_id=rollout.rollout_id,
            causal_status="verified",
            reason_codes=[],
            incumbent_samples=3,
            candidate_samples=3,
            excluded_noncompliant=0,
            excluded_pre_assignment=0,
            effects={},
            verified_at=NOW,
            verification_id=legacy_id,
        ).model_dump(mode="json")
        db.add(
            RandomizedRolloutVerificationRecord(
                verification_id=legacy_id,
                tenant_id="tenant-a",
                rollout_id=rollout.rollout_id,
                request_digest=legacy_digest,
                report_json=report,
                verified_at=NOW,
                verified_by="legacy",
            )
        )
        db.add(_calibration(800, NOW, verification_id=legacy_id))
        db.commit()
    return engine, legacy_id, report


def test_legacy_confounded_cache_is_not_reused_or_overwritten(legacy_heterogeneous):
    engine, legacy_id, report = legacy_heterogeneous
    with Session(engine) as raw:
        db = _db(raw)
        result = service.verify_retained_randomized_rollout(
            db, "legacy-heterogeneous", bootstrap_samples=100, seed=7, now=NOW
        )
        assert result.causal_status == "inconclusive"
        assert result.verification_id != legacy_id
        assert db.get(RandomizedRolloutVerificationRecord, legacy_id).report_json == report
        assert len(list(db.scalars(select(ForecastCalibrationRecord)))) == 1


def test_legacy_confounded_calibration_is_quarantined(legacy_heterogeneous):
    engine, _, _ = legacy_heterogeneous
    result = _refresh(engine)
    assert result["forecast_readiness"]["calibration_state"] == "unknown"
    assert "heterogeneous_assignment_probability" in result["reason_codes"]

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from pydantic import ValidationError
import pytest

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.auth.deps import get_current_scoped_db
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
from zeroth.econ.plane.decisioning.schemas import ProbabilisticMigrationRequest
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _request() -> dict[str, object]:
    def observations(source: str, cost: str) -> list[dict[str, object]]:
        return [
            {
                "case_id": f"case-{index}",
                "cohort": "default",
                "cost_usd": cost,
                "latency_ms": 800,
                "accepted": True,
                "critical_error": index == 0,
                "source": source,
            }
            for index in range(100)
        ]

    request: dict[str, object] = {
        "evidence": {
            "workload": "invoice-agent",
            "incumbent_model": "model-a",
            "candidate_model": "model-b",
            "incumbent": observations("production", "1.00"),
            "candidate": observations("replay", "0.50"),
            "period_request_counts": [90, 100, 110],
            "demand_horizon": "month",
            "readiness": {
                "calibration_state": "calibrated",
                "drift_state": "stable",
                "interval_coverage": 0.95,
                "relative_bias": 0,
                "relative_residual_shift": 0,
                "calibration_periods": 8,
            },
        },
        "policy": {
            "min_paired_cases": 30,
            "candidate_shares": [0.25, 1.0],
            "max_quality_drop": 0.02,
            "max_p95_latency_ms": 1000,
            "max_critical_error_rate": 0.05,
            "max_constraint_breach_probability": 0.10,
            "max_cvar_loss_usd": "0",
        },
        "simulations": 400,
        "seed": 17,
    }
    request["calibration_observations"] = [
        {
            "forecast_id": f"{metric}-{index}",
            "metric": metric,
            "predicted_mean": 100,
            "predicted_low": 90,
            "predicted_high": 110,
            "observed": 100,
            "observed_at": f"2026-0{index + 1}-01T00:00:00Z",
        }
        for metric in (
            "monthly_cost_usd",
            "success_rate",
            "p95_latency_ms",
            "critical_error_rate",
        )
        for index in range(6)
    ]
    return request


def test_forecast_algorithm_version_changes_digest_without_rewriting_history(tmp_path, monkeypatch):
    from zeroth.econ.plane.decisioning import service

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'versions.db'}")
    Base.metadata.create_all(engine)
    request = ProbabilisticMigrationRequest.model_validate(_request())
    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-version"))
        first = service.evaluate_and_retain_probabilistic_migration(
            db, request, evaluated_by="test"
        )
        original = db.get(ProbabilisticMigrationDecisionRecord, first.decision_id)
        original_json = dict(original.report_json)
        monkeypatch.setattr(
            service, "FORECAST_ALGORITHM_VERSION", "future-test-version", raising=False
        )
        second = service.evaluate_and_retain_probabilistic_migration(
            db, request, evaluated_by="test"
        )
        self_repeat = service.evaluate_and_retain_probabilistic_migration(
            db, request, evaluated_by="test"
        )
        assert first.decision_id != second.decision_id
        assert second.decision_id == self_repeat.decision_id
        assert (
            db.get(ProbabilisticMigrationDecisionRecord, first.decision_id).report_json
            == original_json
        )


def test_math_revision_retains_new_identity_and_reads_unchanged_legacy_record(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    from zeroth.econ import probabilistic
    from zeroth.econ.plane.decisioning import service

    old_version = "nested-paired-monthly-v2-hoeffding99"
    expected_version = "nested-paired-monthly-v3-hoeffding99-math1-predictive1"
    assert expected_version == probabilistic.FORECAST_ALGORITHM_VERSION
    assert service.FORECAST_ALGORITHM_VERSION == probabilistic.FORECAST_ALGORITHM_VERSION
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'math-version.db'}")
    Base.metadata.create_all(engine)
    request = ProbabilisticMigrationRequest.model_validate(_request())
    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-math-version"))
        # Simulate an existing record's version identity, not legacy numerical output.
        with monkeypatch.context() as legacy_version:
            legacy_version.setattr(service, "FORECAST_ALGORITHM_VERSION", old_version)
            legacy_version.setattr(probabilistic, "FORECAST_ALGORITHM_VERSION", old_version)
            legacy = service.evaluate_and_retain_probabilistic_migration(
                db, request, evaluated_by="test"
            )
        old_record = db.get(ProbabilisticMigrationDecisionRecord, legacy.decision_id)
        old_json = deepcopy(old_record.report_json)
        old_digest = old_record.request_digest
        repaired = service.evaluate_and_retain_probabilistic_migration(
            db, request, evaluated_by="test"
        )
        repeated = service.evaluate_and_retain_probabilistic_migration(
            db, request, evaluated_by="test"
        )
        assert repaired.decision_id != legacy.decision_id
        assert repaired.decision_id == repeated.decision_id
        assert repaired.evidence_lineage["forecast_algorithm_version"] == expected_version
        history = {
            row.decision_id: row for row in service.list_probabilistic_migration_decisions(db)
        }
        assert set(history) == {legacy.decision_id, repaired.decision_id}
        assert (
            history[legacy.decision_id].evidence_lineage["forecast_algorithm_version"]
            == old_version
        )
        preserved = db.get(ProbabilisticMigrationDecisionRecord, legacy.decision_id)
        assert preserved.report_json == old_json
        assert preserved.request_digest == old_digest


def test_model_migration_route_retains_an_immutable_tenant_decision(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'probabilistic.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    token = mint_econ_service_token()
    assert token is not None
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    response = client.post("/v1/decisions/model-migration", headers=headers, json=_request())
    repeated = client.post("/v1/decisions/model-migration", headers=headers, json=_request())
    contradictory = _request()
    contradictory["evidence"]["readiness"] = {
        "calibration_state": "critical",
        "drift_state": "critical",
        "calibration_periods": 999,
    }
    normalized = client.post("/v1/decisions/model-migration", headers=headers, json=contradictory)
    history = client.get("/v1/decisions/model-migrations", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    rollout = client.post(
        "/v1/randomized-rollouts",
        headers=headers,
        json={
            "decision_id": payload["decision_id"] if response.status_code == 200 else "missing",
            "candidate_probability": 0.5,
            "minimum_per_arm": 20,
        },
    )
    rollout_id = rollout.json().get("rollout_id", "missing")
    assignment = client.post(
        f"/v1/randomized-rollouts/{rollout_id}/assignments",
        headers=headers,
        json={"subject_id": "customer-7", "cohort": "enterprise"},
    )
    verification = client.post(
        f"/v1/randomized-rollouts/{rollout_id}/verify",
        headers=headers,
        json={"bootstrap_samples": 100},
    )

    assert payload["verdict"] == "abstain"
    assert payload["recommended_action"] == "collect_evidence"
    assert payload["recommended_candidate_share"] == 0
    assert "experimental_predictive_reliability_unapproved" in payload["reason_codes"]
    assert "risk_law_unqualified" in payload["reason_codes"]
    assert payload["evidence_lineage"]["predictive_reliability"] == "unapproved"
    assert payload["actions"] == []
    assert "risk_law_unqualified" in payload["reason_codes"]
    assert payload["decision_id"].startswith("pdec_")
    assert payload["evaluated_at"]
    assert repeated.status_code == 200
    assert repeated.json()["decision_id"] == payload["decision_id"]
    assert normalized.status_code == 200
    assert normalized.json()["decision_id"] == payload["decision_id"]
    assert history.status_code == 200
    assert [item["decision_id"] for item in history.json()] == [payload["decision_id"]]
    assert rollout.status_code == 409, rollout.text
    assert assignment.status_code == 404, assignment.text
    assert verification.status_code == 404, verification.text

    with Session(engine) as db:
        assert (
            db.scalar(select(func.count()).select_from(ProbabilisticMigrationDecisionRecord)) == 1
        )
        record = db.scalar(select(ProbabilisticMigrationDecisionRecord))
        assert record is not None
        assert record.evidence_json["period_request_counts"] == [90, 100, 110]
        assert record.evidence_json["demand_horizon"] == "month"
        assert record.evidence_lineage_json["predictive_reliability"] == "unapproved"
        assert record.policy_json["max_constraint_breach_probability"] == 0.1


def test_model_migration_route_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    response = TestClient(app).post("/v1/decisions/model-migration", json=_request())

    assert response.status_code in {401, 403}


def test_model_migration_route_rejects_excessive_simulation_work() -> None:
    request = _request()
    request["simulations"] = 10_001

    with pytest.raises(ValidationError, match="less than or equal to 10000"):
        ProbabilisticMigrationRequest.model_validate(request)


def test_cloud_derives_calibration_instead_of_trusting_a_client_readiness_claim(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'calibration-boundary.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    token = mint_econ_service_token()
    assert token is not None
    request = _request()
    request["calibration_observations"] = []

    response = TestClient(app).post(
        "/v1/decisions/model-migration",
        headers={"Authorization": f"Bearer {token}"},
        json=request,
    )

    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "abstain"
    assert response.json()["reason_codes"] == ["forecast_not_calibrated"]
    assert response.json()["forecast_readiness"]["calibration_state"] == "unknown"


@pytest.mark.parametrize("variant", ["legacy_horizon", "missing_critical", "override"])
def test_public_cutoff_and_missingness_survive_api_storage_roundtrip(
    tmp_path: Path,
    monkeypatch,
    variant: str,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cutoff.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    request = _request()
    if variant == "legacy_horizon":
        del request["evidence"]["demand_horizon"]
    elif variant == "missing_critical":
        del request["evidence"]["candidate"][0]["critical_error"]
    else:
        request["policy"]["require_calibrated_forecast"] = False
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {mint_econ_service_token()}"}
    response = client.post("/v1/decisions/model-migration", headers=headers, json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verdict"] == "abstain"
    assert result["recommended_action"] == "collect_evidence"
    assert result["recommended_candidate_share"] == 0
    assert result["actions"] == []
    if variant == "override":
        assert "risk_law_unqualified" in result["reason_codes"]
        assert result["evidence_lineage"]["predictive_reliability"] == "unapproved"
    with Session(engine) as db:
        record = db.get(ProbabilisticMigrationDecisionRecord, result["decision_id"])
        stored_before = dict(record.evidence_json)
        if variant == "missing_critical":
            assert "critical_error" not in stored_before["candidate"][0]
        if variant == "legacy_horizon":
            assert stored_before["demand_horizon"] == "unknown"
    assert client.get("/v1/decisions/model-migrations", headers=headers).json() == [result]
    repeated = client.post("/v1/decisions/model-migration", headers=headers, json=request)
    assert repeated.json() == result
    with Session(engine) as db:
        assert (
            db.get(ProbabilisticMigrationDecisionRecord, result["decision_id"]).evidence_json
            == stored_before
        )

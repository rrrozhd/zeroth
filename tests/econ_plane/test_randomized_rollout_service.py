from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.cloud import authkit
from zeroth.econ.plane.cloud.keys_api import router as keys_router
from zeroth.econ.plane.cloud.models import CloudIdentityMembership
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base, get_db
from zeroth.econ.plane.decisioning.api import router as decisioning_router
from zeroth.econ.plane.decisioning.models import (
    ForecastCalibrationRecord,
    ProbabilisticMigrationDecisionRecord,
    RandomizedRolloutAssignmentRecord,
)
from zeroth.econ.plane.decisioning.schemas import (
    ProbabilisticMigrationRequest,
    RandomizedRolloutCreate,
)
from zeroth.econ.plane.decisioning.service import (
    RandomizedRolloutInactiveError,
    assign_randomized_rollout,
    create_randomized_rollout,
    stop_randomized_rollout,
    verify_retained_randomized_rollout,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext
from tests.econ_plane.test_workos_authkit import _FakeWorkOS


def _migration_request() -> ProbabilisticMigrationRequest:
    def observations(model: str, cost: str) -> list[dict[str, object]]:
        return [
            {
                "case_id": f"case-{index}",
                "cost_usd": cost,
                "latency_ms": 500 if model == "model-b" else 700,
                "accepted": True,
                "critical_error": index == 0,
                "source": "replay",
            }
            for index in range(100)
        ]

    return ProbabilisticMigrationRequest.model_validate(
        {
            "evidence": {
                "workload": "invoice-agent",
                "incumbent_model": "model-a",
                "candidate_model": "model-b",
                "incumbent": observations("model-a", "1.00"),
                "candidate": observations("model-b", "0.50"),
                "period_request_counts": [100],
            },
            "policy": {
                "min_paired_cases": 30,
                "candidate_shares": [0.5],
                "max_critical_error_rate": 0.05,
                "max_p95_latency_ms": 1000,
                "require_calibrated_forecast": False,
            },
            "simulations": 200,
            "seed": 7,
        }
    )


@pytest.mark.parametrize("heterogeneous", [False, True])
def test_randomized_rollout_is_sticky_verified_and_feeds_calibration_history(
    tmp_path: Path,
    heterogeneous: bool,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'rollout.db'}")
    Base.metadata.create_all(engine)
    assigned_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        # Retained legacy/preauthorized fixture: do not bypass the new public
        # experimental cutoff to create a fresh recommended decision.
        request = _migration_request()
        action = {
            "action_id": "legacy-half",
            "candidate_share": 0.5,
            "expected_monthly_cost_usd": "75",
            "monthly_cost_p05_usd": "75",
            "monthly_cost_p95_usd": "75",
            "expected_monthly_savings_usd": "25",
            "monthly_savings_p05_usd": "25",
            "monthly_savings_p95_usd": "25",
            "probability_negative_savings": 0,
            "probability_quality_breach": 0,
            "probability_latency_breach": 0,
            "probability_critical_error_breach": 0,
            "value_at_risk_usd": "-25",
            "cvar_loss_usd": "-25",
            "minimum_quality_drop_tolerance": 0,
            "minimum_p95_latency_limit_ms": 700,
            "minimum_critical_error_rate_limit": 0.01,
            "minimum_cvar_loss_limit_usd": "-25",
            "feasible": True,
            "violated_constraints": [],
            "expected_success_rate": 1,
            "success_rate_p05": 1,
            "success_rate_p95": 1,
            "expected_p95_latency_ms": 700,
            "p95_latency_p05_ms": 700,
            "p95_latency_p95_ms": 700,
            "expected_critical_error_rate": 0.01,
            "critical_error_rate_p05": 0.01,
            "critical_error_rate_p95": 0.01,
        }
        report = {
            "workload": "invoice-agent",
            "incumbent_model": "model-a",
            "candidate_model": "model-b",
            "verdict": "recommend",
            "recommended_action": "hybrid_route",
            "recommended_candidate_share": 0.5,
            "reason_codes": ["legacy_preapproved_fixture"],
            "simulations": 200,
            "seed": 7,
            "actions": [action],
            "forecast_readiness": {"calibration_state": "calibrated", "drift_state": "stable"},
        }
        decision = ProbabilisticMigrationDecisionRecord(
            decision_id="legacy-approved-decision",
            tenant_id="tenant-a",
            request_digest="a" * 64,
            workload="invoice-agent",
            incumbent_model="model-a",
            candidate_model="model-b",
            verdict="recommend",
            recommended_action="hybrid_route",
            evidence_json=request.evidence.model_dump(mode="json"),
            evidence_lineage_json={},
            policy_json=request.policy.model_dump(mode="json"),
            report_json=report,
            evaluated_at=assigned_at - timedelta(days=1),
            evaluated_by="legacy-approver@example.com",
        )
        db.add(decision)
        db.commit()
        rollout_request = RandomizedRolloutCreate(
            decision_id=decision.decision_id,
            candidate_probability=0.5,
            minimum_per_arm=100,
            cohort_candidate_probabilities={"other": 0.8} if heterogeneous else {},
        )
        rollout = create_randomized_rollout(
            db,
            rollout_request,
            created_by="analyst@example.com",
            now=assigned_at,
        )
        assignments = []
        for index in range(500):
            assignment = assign_randomized_rollout(
                db,
                rollout.rollout_id,
                subject_id=f"subject-{index}",
                cohort="default",
                now=assigned_at,
            )
            repeated = assign_randomized_rollout(
                db,
                rollout.rollout_id,
                subject_id=f"subject-{index}",
                cohort="default",
                now=assigned_at + timedelta(minutes=1),
            )
            assert repeated == assignment
            assignments.append(assignment)
            join_key = f"run-{index}"
            cost = "0.50" if assignment.arm == "candidate" else "1.00"
            raw.add(
                ExecutionEvent(
                    tenant_id="tenant-a",
                    evidence_kind="production",
                    subject_id=assignment.subject_id,
                    dimensions={"cohort": "default"},
                    execution_id=f"exec-{index}",
                    join_key=join_key,
                    timestamp=assigned_at + timedelta(minutes=2),
                    capability_id="invoice-agent",
                    implementation_id="v1",
                    model_version=assignment.assigned_model,
                    token_cost_usd=Decimal(cost),
                    tool_cost_usd=Decimal("0"),
                    compute_cost_usd=Decimal("0"),
                    cost_measurement="measured",
                    usage_measurement="measured",
                    latency_ms=500,
                    compute_time_ms=0,
                    event_metadata={},
                )
            )
            raw.add(
                OutcomeEvent(
                    tenant_id="tenant-a",
                    join_key=join_key,
                    execution_id="",
                    capability_id="invoice-agent",
                    implementation_id="v1",
                    outcome_type="accepted",
                    outcome_payload_json={"accepted": True},
                    outcome_value="true",
                    occurred_at=assigned_at + timedelta(minutes=2),
                    ingested_at=assigned_at + timedelta(minutes=2),
                    outcome_timestamp=assigned_at + timedelta(minutes=2),
                    provenance="MEASURED",
                )
            )
        raw.commit()

        stopped = stop_randomized_rollout(db, rollout.rollout_id)
        repeated_stop = stop_randomized_rollout(db, rollout.rollout_id)
        assert stopped.active is False
        assert repeated_stop == stopped
        assert (
            assign_randomized_rollout(
                db,
                rollout.rollout_id,
                subject_id="subject-0",
                cohort="default",
                now=assigned_at + timedelta(minutes=3),
            )
            == assignments[0]
        )
        with pytest.raises(RandomizedRolloutInactiveError):
            assign_randomized_rollout(
                db,
                rollout.rollout_id,
                subject_id="post-stop-subject",
                cohort="default",
                now=assigned_at + timedelta(minutes=3),
            )

        verification = verify_retained_randomized_rollout(
            db,
            rollout.rollout_id,
            outcome_type="accepted",
            bootstrap_samples=200,
            seed=17,
            now=assigned_at + timedelta(hours=1),
        )
        repeated_verification = verify_retained_randomized_rollout(
            db,
            rollout.rollout_id,
            outcome_type="accepted",
            bootstrap_samples=200,
            seed=17,
            now=assigned_at + timedelta(hours=1),
        )

        assignment_count = len(list(raw.scalars(select(RandomizedRolloutAssignmentRecord))))
        calibration = list(raw.scalars(select(ForecastCalibrationRecord)))
        decision_record = raw.scalar(
            select(ProbabilisticMigrationDecisionRecord).where(
                ProbabilisticMigrationDecisionRecord.decision_id == decision.decision_id
            )
        )

    # The retained OSS state must not authorize new paid causal operations.
    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")
    app.include_router(keys_router, prefix="/v1")

    def raw_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = raw_db
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    monkeypatch.setattr(settings, "service_principal_roles", "Admin")
    monkeypatch.setattr(settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(settings, "cloud_browser_origin", "https://app.example.test")
    monkeypatch.setattr(authkit, "get_workos_gateway", lambda: _FakeWorkOS())
    with Session(engine) as db:
        db.add(CloudIdentityMembership(
            tenant_id="tenant-a", provider="workos", external_user_id="user_01",
            external_organization_id="org_01", email="owner@example.com",
            created_at=assigned_at, updated_at=assigned_at,
        ))
        db.commit()
    token = mint_econ_service_token()
    assert token is not None
    jwt_headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app, base_url="https://api.example.test")
    created_key = client.post(
        "/v1/cloud/api-keys", headers=jwt_headers,
        json={"name": "test", "roles": ["Admin"]},
    )
    assert created_key.status_code == 200, created_key.text
    operations = [
        ("/v1/randomized-rollouts",
         rollout_request.model_dump(mode="json") | {"candidate_probability": 0.25}),
        (f"/v1/randomized-rollouts/{rollout.rollout_id}/assignments",
         {"subject_id": "subject-0"}),
        (f"/v1/randomized-rollouts/{rollout.rollout_id}/verify",
         {"bootstrap_samples": 200, "seed": 17}),
    ]
    denied = {}
    for mode in ("api_key", "browser"):
        client.cookies.clear()
        if mode == "api_key":
            headers = {"Authorization": f"Bearer {created_key.json()['api_key']}"}
        else:
            client.cookies.set("zeroth_session", "sealed-session")
            headers = {"Origin": "https://app.example.test"}
        history = client.get("/v1/decisions/model-migrations", headers=headers)
        assert history.status_code == 200, history.text
        assert history.json()[0]["decision_id"] == "legacy-approved-decision"
        denied[mode] = [
            client.post(path, json=body, headers=headers).status_code
            for path, body in operations
        ]
        stopped_response = client.post(
            f"/v1/randomized-rollouts/{rollout.rollout_id}/stop", headers=headers,
        )
        assert stopped_response.status_code == 200, stopped_response.text
    assert all(status in {401, 403} for statuses in denied.values() for status in statuses), denied

    client.cookies.clear()
    for path, body in operations:
        legacy = client.post(path, json=body, headers=jwt_headers)
        assert legacy.status_code == 200, legacy.text
    assert legacy.json()["causal_status"] == verification.causal_status
    assert assignment_count == 500
    assert verification.verification_id == repeated_verification.verification_id
    if heterogeneous:
        assert verification.causal_status == "inconclusive"
        assert verification.reason_codes == ["heterogeneous_assignment_probability"]
        assert verification.effects == {}
        assert calibration == []
        return
    assert verification.causal_status == "verified"
    assert verification.effects["cost_usd"].estimated_difference == -0.5
    assert {row.metric for row in calibration} == {
        "monthly_cost_usd",
        "success_rate",
        "p95_latency_ms",
        "critical_error_rate",
    }
    assert decision_record is not None

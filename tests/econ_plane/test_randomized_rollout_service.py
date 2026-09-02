from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zeroth.econ.plane.database import Base
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
    assign_randomized_rollout,
    create_randomized_rollout,
    evaluate_and_retain_probabilistic_migration,
    verify_retained_randomized_rollout,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


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


def test_randomized_rollout_is_sticky_verified_and_feeds_calibration_history(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'rollout.db'}")
    Base.metadata.create_all(engine)
    assigned_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with Session(engine) as raw:
        db = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        decision = evaluate_and_retain_probabilistic_migration(
            db, _migration_request(), evaluated_by="analyst@example.com"
        )
        rollout = create_randomized_rollout(
            db,
            RandomizedRolloutCreate(
                decision_id=decision.decision_id,
                candidate_probability=0.5,
                minimum_per_arm=100,
            ),
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

    assert assignment_count == 500
    assert verification.verification_id == repeated_verification.verification_id
    assert verification.causal_status == "verified"
    assert verification.effects["cost_usd"].estimated_difference == -0.5
    assert {row.metric for row in calibration} == {
        "monthly_cost_usd",
        "success_rate",
        "p95_latency_ms",
        "critical_error_rate",
    }
    assert decision_record is not None

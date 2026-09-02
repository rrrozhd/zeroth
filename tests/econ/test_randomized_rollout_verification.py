from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from zeroth.econ.rollout_verification import (
    RandomizedRolloutPlan,
    RolloutObservation,
    assign_rollout_arm,
    verify_randomized_rollout,
)


def test_assignment_is_sticky_reproducible_and_approximately_balanced() -> None:
    plan = RandomizedRolloutPlan(
        rollout_id="rollout-1",
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        candidate_probability=0.5,
        assignment_salt="secret-random-salt",
    )

    assignments = [assign_rollout_arm(plan, f"subject-{index}") for index in range(1_000)]

    assigned_at = datetime(2026, 9, 2, tzinfo=UTC)
    assert assign_rollout_arm(plan, "subject-7", assigned_at=assigned_at) == assign_rollout_arm(
        plan, "subject-7", assigned_at=assigned_at
    )
    candidate_count = sum(row.arm == "candidate" for row in assignments)
    assert 450 <= candidate_count <= 550
    assert all(
        row.assigned_model
        == (plan.candidate_model if row.arm == "candidate" else plan.incumbent_model)
        for row in assignments
    )


def test_verification_estimates_randomized_effects_and_excludes_noncompliance() -> None:
    assigned_at = datetime(2026, 9, 2, tzinfo=UTC)
    plan = RandomizedRolloutPlan(
        rollout_id="rollout-1",
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        candidate_probability=0.5,
        assignment_salt="secret-random-salt",
    )
    assignments = [
        assign_rollout_arm(plan, f"subject-{index}", assigned_at=assigned_at)
        for index in range(400)
    ]
    observations = [
        RolloutObservation(
            subject_id=row.subject_id,
            model_used=row.assigned_model,
            cost_usd=Decimal("0.50") if row.arm == "candidate" else Decimal("1.00"),
            latency_ms=500 if row.arm == "candidate" else 700,
            accepted=True,
            critical_error=False,
            observed_at=assigned_at + timedelta(minutes=1),
        )
        for row in assignments
    ]
    observations.append(
        RolloutObservation(
            subject_id=assignments[0].subject_id,
            model_used="wrong-model",
            cost_usd=Decimal("100"),
            latency_ms=99_000,
            accepted=False,
            critical_error=True,
            observed_at=assigned_at + timedelta(minutes=2),
        )
    )

    report = verify_randomized_rollout(
        plan,
        assignments,
        observations,
        minimum_per_arm=100,
        bootstrap_samples=500,
        seed=19,
    )

    assert report.causal_status == "invalid"
    assert report.reason_codes == ["assignment_noncompliance_detected"]
    assert report.excluded_noncompliant == 1
    assert report.effects["cost_usd"].estimated_difference == Decimal("-0.5")
    assert report.effects["latency_ms"].estimated_difference == -200
    assert report.effects["success_rate"].estimated_difference == 0
    assert report.effects["cost_usd"].confidence_high < 0


def test_verification_is_inconclusive_when_an_arm_is_underpowered() -> None:
    plan = RandomizedRolloutPlan(
        rollout_id="rollout-1",
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        candidate_probability=0.01,
        assignment_salt="secret-random-salt",
    )
    assigned_at = datetime(2026, 9, 2, tzinfo=UTC)
    assignments = [
        assign_rollout_arm(plan, f"subject-{index}", assigned_at=assigned_at) for index in range(50)
    ]
    observations = [
        RolloutObservation(
            subject_id=row.subject_id,
            model_used=row.assigned_model,
            cost_usd=Decimal("1"),
            latency_ms=500,
            accepted=True,
            observed_at=assigned_at + timedelta(minutes=1),
        )
        for row in assignments
    ]

    report = verify_randomized_rollout(
        plan, assignments, observations, minimum_per_arm=20, bootstrap_samples=100
    )

    assert report.causal_status == "inconclusive"
    assert "minimum_arm_sample_not_met" in report.reason_codes

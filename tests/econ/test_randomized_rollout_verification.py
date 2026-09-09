from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import zeroth.econ.rollout_verification as subject
from zeroth.econ.rollout_verification import (
    RandomizedRolloutPlan,
    RolloutAssignment,
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


@pytest.mark.parametrize("heterogeneous", [False, True])
def test_cohort_weighted_assignment_does_not_claim_a_pooled_causal_effect(heterogeneous):
    when = datetime(2026, 9, 2, tzinfo=UTC)
    plan = RandomizedRolloutPlan(
        rollout_id="cohorts",
        workload="test",
        incumbent_model="a",
        candidate_model="b",
        candidate_probability=0.5,
        assignment_salt="synthetic-test-salt",
        cohort_candidate_probabilities={
            "high": 0.9 if heterogeneous else 0.5,
            "low": 0.1 if heterogeneous else 0.5,
        },
    )
    assignments, observations = [], []
    for i in range(500):
        cohort = "high" if i < 250 else "low"
        assigned = assign_rollout_arm(plan, str(i), cohort=cohort, assigned_at=when)
        assignments.append(assigned)
        observations.append(
            RolloutObservation(
                subject_id=str(i),
                model_used=assigned.assigned_model,
                cost_usd=Decimal(10 if cohort == "high" else 1),
                latency_ms=500,
                accepted=True,
                critical_error=False,
                observed_at=when + timedelta(minutes=1),
            )
        )
    result = verify_randomized_rollout(
        plan,
        assignments,
        observations,
        minimum_per_arm=100,
        bootstrap_samples=100,
        verified_at=when + timedelta(hours=1),
    )
    if heterogeneous:
        assert result.causal_status == "inconclusive"
        assert "heterogeneous_assignment_probability" in result.reason_codes
        assert result.effects == {}
    else:
        assert result.causal_status == "verified"


def test_verification_fails_closed_on_assignment_noncompliance(monkeypatch) -> None:
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
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(AssertionError("RNG initialized after noncompliance")),
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
    assert report.effects == {}


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


def test_empty_assignment_set_keeps_minimum_arm_abstention_before_rng(monkeypatch):
    plan, _, _ = _balanced_fixture()
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(
            AssertionError("RNG initialized for empty assignment set")
        ),
    )

    result = verify_randomized_rollout(plan, [], [], minimum_per_arm=10, bootstrap_samples=100)

    assert result.causal_status == "inconclusive"
    assert result.reason_codes == ["minimum_arm_sample_not_met"]
    assert result.effects == {}


def _balanced_fixture(size_per_arm: int = 20):
    assigned_at = datetime(2026, 9, 2, tzinfo=UTC)
    plan = RandomizedRolloutPlan(
        rollout_id="integrity-test",
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        candidate_probability=0.5,
        assignment_salt="integrity-test-salt",
    )
    assignments = [
        RolloutAssignment(
            subject_id=f"incumbent-{index}",
            arm="incumbent",
            assigned_model="model-a",
            assigned_at=assigned_at,
        )
        for index in range(size_per_arm)
    ] + [
        RolloutAssignment(
            subject_id=f"candidate-{index}",
            arm="candidate",
            assigned_model="model-b",
            assigned_at=assigned_at,
        )
        for index in range(size_per_arm)
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
    return plan, assignments, observations


def test_exact_two_sided_binomial_pvalue_includes_probability_mass_ties():
    assert subject._exact_two_sided_binomial_pvalue(0, 2, 0.5) == 0.5
    assert subject._exact_two_sided_binomial_pvalue(1, 2, 0.5) == 1.0


def test_balanced_assignment_ratio_is_not_rejected():
    plan, assignments, observations = _balanced_fixture()

    result = verify_randomized_rollout(
        plan,
        assignments,
        observations,
        minimum_per_arm=10,
        bootstrap_samples=100,
    )

    assert result.causal_status == "verified"
    assert result.effects
    assert "assignment_ratio_mismatch" not in result.reason_codes


def test_extreme_assignment_ratio_fails_closed_before_rng(monkeypatch):
    plan, assignments, observations = _balanced_fixture()
    assignments = [
        replace.model_copy(update={"arm": "candidate", "assigned_model": "model-b"})
        for replace in assignments
    ]
    observations = [
        observation.model_copy(update={"model_used": "model-b"}) for observation in observations
    ]
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(AssertionError("RNG initialized after SRM")),
    )

    result = verify_randomized_rollout(
        plan, assignments, observations, minimum_per_arm=1, bootstrap_samples=100
    )

    assert result.causal_status == "invalid"
    assert result.reason_codes == ["assignment_ratio_mismatch"]
    assert result.effects == {}


@pytest.mark.parametrize("missing_kind", ["candidate_only", "both_arms"])
def test_any_missing_post_assignment_outcome_fails_closed(monkeypatch, missing_kind):
    plan, assignments, observations = _balanced_fixture()
    missing = {"candidate-0"}
    if missing_kind == "both_arms":
        missing.add("incumbent-0")
    observations = [row for row in observations if row.subject_id not in missing]
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(AssertionError("RNG initialized after attrition")),
    )

    result = verify_randomized_rollout(
        plan, assignments, observations, minimum_per_arm=10, bootstrap_samples=100
    )

    assert result.causal_status == "inconclusive"
    assert "outcome_attrition_detected" in result.reason_codes
    assert result.effects == {}


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_or_conflicting_assignment_fails_closed_before_rng(monkeypatch, conflicting):
    plan, assignments, observations = _balanced_fixture()
    duplicate = assignments[0]
    if conflicting:
        duplicate = duplicate.model_copy(update={"arm": "candidate", "assigned_model": "model-b"})
    assignments.append(duplicate)
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(
            AssertionError("RNG initialized after duplicate assignment")
        ),
    )

    result = verify_randomized_rollout(
        plan, assignments, observations, minimum_per_arm=10, bootstrap_samples=100
    )

    assert result.causal_status == "invalid"
    assert result.reason_codes == ["duplicate_assignment_detected"]
    assert result.effects == {}


def test_heterogeneous_propensity_fails_closed_before_rng(monkeypatch):
    plan, assignments, observations = _balanced_fixture()
    plan = plan.model_copy(update={"cohort_candidate_probabilities": {"high": 0.9}})
    monkeypatch.setattr(
        subject.random,
        "Random",
        lambda _seed: (_ for _ in ()).throw(
            AssertionError("RNG initialized after heterogeneous propensity")
        ),
    )

    result = verify_randomized_rollout(
        plan, assignments, observations, minimum_per_arm=10, bootstrap_samples=100
    )

    assert result.causal_status == "inconclusive"
    assert result.reason_codes == ["heterogeneous_assignment_probability"]
    assert result.effects == {}

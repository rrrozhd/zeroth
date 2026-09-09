"""Randomized rollout assignment and conservative causal verification."""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from math import comb
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RandomizedRolloutPlan(BaseModel):
    """Assignment configuration for a salted incumbent-versus-candidate rollout."""

    model_config = ConfigDict(extra="forbid")

    rollout_id: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    candidate_probability: float = Field(gt=0, lt=1)
    assignment_salt: str = Field(min_length=16, max_length=256, exclude=True)
    cohort_candidate_probabilities: dict[str, float] = Field(default_factory=dict)


class RolloutAssignment(BaseModel):
    """A subject's immutable randomized arm and model assignment."""

    model_config = ConfigDict(extra="forbid")

    subject_id: str
    arm: Literal["incumbent", "candidate"]
    assigned_model: str
    assigned_at: datetime


class RolloutObservation(BaseModel):
    """A measured post-assignment outcome eligible for rollout verification."""

    model_config = ConfigDict(extra="forbid")

    subject_id: str
    model_used: str
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    accepted: bool
    critical_error: bool = False
    observed_at: datetime


class CausalMetricEffect(BaseModel):
    """Arm means and bootstrap interval for one intention-compatible metric effect."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    incumbent_mean: float
    candidate_mean: float
    estimated_difference: float
    confidence_low: float
    confidence_high: float


class RolloutVerification(BaseModel):
    """Fail-closed causal verification result and its retained metric effects."""

    model_config = ConfigDict(extra="forbid")

    rollout_id: str
    causal_status: Literal["verified", "inconclusive", "invalid"]
    reason_codes: list[str]
    incumbent_samples: int
    candidate_samples: int
    excluded_noncompliant: int
    excluded_pre_assignment: int
    effects: dict[str, CausalMetricEffect]
    verified_at: datetime
    verification_id: str | None = None


def assign_rollout_arm(
    plan: RandomizedRolloutPlan,
    subject_id: str,
    *,
    cohort: str = "default",
    assigned_at: datetime | None = None,
) -> RolloutAssignment:
    """Deterministically randomize a stable subject without exposing the salt."""
    digest = hashlib.sha256(
        f"{plan.assignment_salt}:{plan.rollout_id}:{subject_id}".encode()
    ).digest()
    draw = int.from_bytes(digest[:8], "big") / 2**64
    probability = plan.cohort_candidate_probabilities.get(cohort, plan.candidate_probability)
    arm: Literal["incumbent", "candidate"] = "candidate" if draw < probability else "incumbent"
    return RolloutAssignment(
        subject_id=subject_id,
        arm=arm,
        assigned_model=(plan.candidate_model if arm == "candidate" else plan.incumbent_model),
        assigned_at=assigned_at or datetime.now(UTC),
    )


def _effect(
    metric: str,
    incumbent: list[float],
    candidate: list[float],
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> CausalMetricEffect:
    """Estimate an arm-mean difference and percentile bootstrap interval."""
    incumbent_mean = fmean(incumbent)
    candidate_mean = fmean(candidate)
    differences: list[float] = []
    for _ in range(bootstrap_samples):
        control_mean = fmean(rng.choice(incumbent) for _ in incumbent)
        treatment_mean = fmean(rng.choice(candidate) for _ in candidate)
        differences.append(treatment_mean - control_mean)
    ordered = sorted(differences)
    low = ordered[max(0, int(0.025 * len(ordered)))]
    high = ordered[min(len(ordered) - 1, int(0.975 * len(ordered)))]
    return CausalMetricEffect(
        metric=metric,
        incumbent_mean=incumbent_mean,
        candidate_mean=candidate_mean,
        estimated_difference=candidate_mean - incumbent_mean,
        confidence_low=low,
        confidence_high=high,
    )


def _exact_two_sided_binomial_probability(
    candidate_count: int, total: int, candidate_probability: float
) -> Fraction:
    """Exact probability-ordering two-sided binomial p-value, including ties."""
    if type(candidate_count) is not int or type(total) is not int:
        raise TypeError("candidate_count and total must be integers")
    if total < 1 or not 0 <= candidate_count <= total:
        raise ValueError("candidate_count must be between zero and a positive total")
    if not 0 < candidate_probability < 1:
        raise ValueError("candidate_probability must be between zero and one")
    probability = Fraction.from_float(candidate_probability)
    numerator = probability.numerator
    denominator = probability.denominator
    complement = denominator - numerator

    def mass_numerator(count: int) -> int:
        return comb(total, count) * numerator**count * complement ** (total - count)

    masses = [mass_numerator(count) for count in range(total + 1)]
    observed_mass = masses[candidate_count]
    included_mass = sum(mass for mass in masses if mass <= observed_mass)
    return Fraction(included_mass, denominator**total)


def _exact_two_sided_binomial_pvalue(
    candidate_count: int, total: int, candidate_probability: float
) -> float:
    """Expose the exact probability-ordering binomial result as a float."""
    return float(
        _exact_two_sided_binomial_probability(candidate_count, total, candidate_probability)
    )


def verify_randomized_rollout(
    plan: RandomizedRolloutPlan,
    assignments: list[RolloutAssignment],
    observations: list[RolloutObservation],
    *,
    minimum_per_arm: int = 100,
    bootstrap_samples: int = 2_000,
    seed: int = 7,
    verified_at: datetime | None = None,
) -> RolloutVerification:
    """Estimate intention-compatible arm effects from post-assignment observations."""
    if minimum_per_arm < 1:
        raise ValueError("minimum_per_arm must be positive")
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    subject_ids = [row.subject_id for row in assignments]
    if len(subject_ids) != len(set(subject_ids)):
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="invalid",
            reason_codes=["duplicate_assignment_detected"],
            incumbent_samples=0,
            candidate_samples=0,
            excluded_noncompliant=0,
            excluded_pre_assignment=0,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    assignment_by_subject = {row.subject_id: row for row in assignments}
    selected: dict[str, RolloutObservation] = {}
    excluded_noncompliant = 0
    excluded_pre_assignment = 0
    for observation in sorted(observations, key=lambda row: row.observed_at):
        assignment = assignment_by_subject.get(observation.subject_id)
        if assignment is None:
            excluded_noncompliant += 1
            continue
        if observation.observed_at < assignment.assigned_at:
            excluded_pre_assignment += 1
            continue
        if observation.model_used != assignment.assigned_model:
            excluded_noncompliant += 1
            continue
        selected.setdefault(observation.subject_id, observation)
    by_arm: dict[str, list[RolloutObservation]] = {"incumbent": [], "candidate": []}
    for subject_id, observation in selected.items():
        by_arm[assignment_by_subject[subject_id].arm].append(observation)
    incumbent = by_arm["incumbent"]
    candidate = by_arm["candidate"]
    attrition = len(selected) != len(assignments)
    probabilities = {plan.candidate_probability, *plan.cohort_candidate_probabilities.values()}
    if len(probabilities) > 1:
        # No retained propensity/cohort strata exist for a valid adjusted
        # estimator. Include the default fallback even if it was not observed.
        reasons = ["heterogeneous_assignment_probability"]
        if excluded_noncompliant:
            reasons.append("assignment_noncompliance_detected")
        if attrition:
            reasons.append("outcome_attrition_detected")
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="invalid" if excluded_noncompliant else "inconclusive",
            reason_codes=reasons,
            incumbent_samples=len(incumbent),
            candidate_samples=len(candidate),
            excluded_noncompliant=excluded_noncompliant,
            excluded_pre_assignment=excluded_pre_assignment,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    if excluded_noncompliant or attrition:
        reasons = []
        if excluded_noncompliant:
            reasons.append("assignment_noncompliance_detected")
        if attrition:
            reasons.append("outcome_attrition_detected")
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="invalid" if excluded_noncompliant else "inconclusive",
            reason_codes=reasons,
            incumbent_samples=len(incumbent),
            candidate_samples=len(candidate),
            excluded_noncompliant=excluded_noncompliant,
            excluded_pre_assignment=excluded_pre_assignment,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    if not assignments:
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="inconclusive",
            reason_codes=["minimum_arm_sample_not_met"],
            incumbent_samples=0,
            candidate_samples=0,
            excluded_noncompliant=excluded_noncompliant,
            excluded_pre_assignment=excluded_pre_assignment,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    assignment_candidate_count = sum(row.arm == "candidate" for row in assignments)
    if _exact_two_sided_binomial_probability(
        assignment_candidate_count,
        len(assignments),
        plan.candidate_probability,
    ) <= Fraction(1, 1_000):
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="invalid",
            reason_codes=["assignment_ratio_mismatch"],
            incumbent_samples=len(incumbent),
            candidate_samples=len(candidate),
            excluded_noncompliant=excluded_noncompliant,
            excluded_pre_assignment=excluded_pre_assignment,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    if len(incumbent) < minimum_per_arm or len(candidate) < minimum_per_arm:
        return RolloutVerification(
            rollout_id=plan.rollout_id,
            causal_status="inconclusive",
            reason_codes=["minimum_arm_sample_not_met"],
            incumbent_samples=len(incumbent),
            candidate_samples=len(candidate),
            excluded_noncompliant=excluded_noncompliant,
            excluded_pre_assignment=excluded_pre_assignment,
            effects={},
            verified_at=verified_at or datetime.now(UTC),
        )
    rng = random.Random(seed)
    vectors = {
        "cost_usd": (
            [float(row.cost_usd) for row in incumbent],
            [float(row.cost_usd) for row in candidate],
        ),
        "latency_ms": (
            [float(row.latency_ms) for row in incumbent],
            [float(row.latency_ms) for row in candidate],
        ),
        "success_rate": (
            [float(row.accepted) for row in incumbent],
            [float(row.accepted) for row in candidate],
        ),
        "critical_error_rate": (
            [float(row.critical_error) for row in incumbent],
            [float(row.critical_error) for row in candidate],
        ),
    }
    effects = {
        metric: _effect(
            metric,
            control,
            treatment,
            bootstrap_samples=bootstrap_samples,
            rng=rng,
        )
        for metric, (control, treatment) in vectors.items()
    }
    return RolloutVerification(
        rollout_id=plan.rollout_id,
        causal_status="verified",
        reason_codes=[],
        incumbent_samples=len(incumbent),
        candidate_samples=len(candidate),
        excluded_noncompliant=excluded_noncompliant,
        excluded_pre_assignment=excluded_pre_assignment,
        effects=effects,
        verified_at=verified_at or datetime.now(UTC),
    )


__all__ = [
    "CausalMetricEffect",
    "RandomizedRolloutPlan",
    "RolloutAssignment",
    "RolloutObservation",
    "RolloutVerification",
    "assign_rollout_arm",
    "verify_randomized_rollout",
]

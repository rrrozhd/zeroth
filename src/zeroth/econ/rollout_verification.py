"""Randomized rollout assignment and conservative causal verification."""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime
from decimal import Decimal
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RandomizedRolloutPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rollout_id: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    candidate_probability: float = Field(gt=0, lt=1)
    assignment_salt: str = Field(min_length=16, max_length=256, exclude=True)
    cohort_candidate_probabilities: dict[str, float] = Field(default_factory=dict)


class RolloutAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str
    arm: Literal["incumbent", "candidate"]
    assigned_model: str
    assigned_at: datetime


class RolloutObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str
    model_used: str
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    accepted: bool
    critical_error: bool = False
    observed_at: datetime


class CausalMetricEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    incumbent_mean: float
    candidate_mean: float
    estimated_difference: float
    confidence_low: float
    confidence_high: float


class RolloutVerification(BaseModel):
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
    invalid = excluded_noncompliant > 0
    return RolloutVerification(
        rollout_id=plan.rollout_id,
        causal_status="invalid" if invalid else "verified",
        reason_codes=["assignment_noncompliance_detected"] if invalid else [],
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

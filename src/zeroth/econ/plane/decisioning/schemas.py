"""API contracts for economic workflow-version decisions."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from zeroth.econ.decisioning import DecisionPolicy
from zeroth.econ.probabilistic import (
    ForecastCalibrationObservation,
    MigrationEvidence,
    MigrationRiskPolicy,
)
from zeroth.econ.rollout_verification import RolloutAssignment, RolloutVerification


class MigrationEvidenceSource(BaseModel):
    """A durable selector from which fresh migration evidence can be rebuilt."""

    model_config = ConfigDict(extra="forbid")

    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    outcome_type: str = Field(default="accepted", min_length=1, max_length=64)
    lookback_days: int = Field(default=30, ge=1, le=366)
    cohort_dimension: str = Field(default="cohort", min_length=1, max_length=128)


class VersionComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)


class ProbabilisticMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: MigrationEvidence
    policy: MigrationRiskPolicy
    calibration_observations: list[ForecastCalibrationObservation] = Field(
        default_factory=list, max_length=200
    )
    simulations: int = Field(default=10_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class MigrationEvidenceRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_source: MigrationEvidenceSource
    policy: MigrationRiskPolicy
    simulations: int = Field(default=10_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class DecisionScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    interval_minutes: int = Field(default=1440, ge=60, le=43_200)


class DecisionScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schedule_id: str
    workflow: str
    baseline_version: str
    candidate_version: str
    outcome_type: str
    policy: DecisionPolicy
    interval_minutes: int
    active: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_decision_id: str | None
    last_error: str | None
    created_at: datetime


class ProbabilisticDecisionScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_source: MigrationEvidenceSource
    policy: MigrationRiskPolicy
    interval_minutes: int = Field(default=1440, ge=60, le=43_200)
    simulations: int = Field(default=10_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class ProbabilisticDecisionScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schedule_id: str
    evidence_source: MigrationEvidenceSource
    policy: MigrationRiskPolicy
    interval_minutes: int
    simulations: int
    seed: int
    active: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_decision_id: str | None
    last_error: str | None
    created_at: datetime


class RandomizedRolloutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=40)
    candidate_probability: float = Field(default=0.5, gt=0, lt=1)
    cohort_candidate_probabilities: dict[str, float] = Field(default_factory=dict)
    minimum_per_arm: int = Field(default=100, ge=20, le=10_000)


class RandomizedRolloutOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rollout_id: str
    decision_id: str
    workload: str
    incumbent_model: str
    candidate_model: str
    candidate_probability: float
    cohort_candidate_probabilities: dict[str, float]
    minimum_per_arm: int
    active: bool
    created_at: datetime


class RandomizedRolloutAssignmentOut(RolloutAssignment):
    model_config = ConfigDict(extra="forbid")

    rollout_id: str


class RandomizedRolloutAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=192)
    cohort: str = Field(default="default", min_length=1, max_length=128)


class RandomizedRolloutVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_type: str = Field(default="accepted", min_length=1, max_length=64)
    bootstrap_samples: int = Field(default=2_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)

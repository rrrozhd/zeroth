"""Transport-safe economic workflow contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class ExecutionEvent(BaseModel):
    """Measured cost and latency for one workflow step."""

    workflow: str = Field(min_length=1)
    workflow_version: str = Field(default="unversioned", min_length=1)
    run_id: str = Field(min_length=1)
    step: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_version: str = Field(default="unknown", min_length=1)
    cost_usd: Decimal | None = Field(default=Decimal("0"), ge=0)
    cost_measurement: Literal["measured", "estimated", "unmeasured"] = "measured"
    latency_ms: int = Field(default=0, ge=0)
    subject_id: str | None = None
    dimensions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cost_matches_measurement(self) -> ExecutionEvent:
        if self.cost_measurement == "unmeasured" and self.cost_usd is not None:
            raise ValueError("unmeasured cost must not include a value")
        if self.cost_measurement != "unmeasured" and self.cost_usd is None:
            raise ValueError("measured or estimated cost requires a value")
        return self


class OutcomeEvent(BaseModel):
    """Business acceptance signal associated with a workflow run."""

    workflow: str = Field(min_length=1)
    workflow_version: str = Field(default="unversioned", min_length=1)
    run_id: str = Field(min_length=1)
    accepted: bool
    outcome_type: str = Field(default="accepted", min_length=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: Literal["measured", "inferred", "mixed"] = "measured"
    value_usd: Decimal | None = Field(default=None, ge=0)
    score: float | None = Field(default=None, ge=0, le=1)
    subject_id: str | None = None
    dimensions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EconomicConstraints(BaseModel):
    """Quality and economic boundaries a backtest candidate must satisfy."""

    min_success_rate: float | None = Field(default=None, ge=0, le=1)
    max_cost_per_outcome_usd: Decimal | None = Field(default=None, ge=0)
    max_critical_error_rate: float | None = Field(default=None, ge=0, le=1)


class BacktestCase(BaseModel):
    """Ephemeral input and expected output used for one bounded replay."""

    id: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(min_length=1)
    expected: dict[str, Any] = Field(min_length=1)


class BacktestRequest(BaseModel):
    """Candidate workflow change and the boundaries used to judge it."""

    workflow: str = Field(min_length=1)
    baseline_version: str | None = Field(default=None, min_length=1)
    node_id: str | None = Field(default=None, min_length=1)
    incumbent_model: str | None = Field(default=None, min_length=1)
    instruction: str | None = Field(default=None, min_length=1)
    candidate: dict[str, Any] = Field(min_length=1)
    cases: list[BacktestCase] = Field(default_factory=list, max_length=25)
    constraints: EconomicConstraints


class DecisionPolicy(BaseModel):
    """Evidence and economic constraints for a workflow-version decision."""

    min_runs: int = Field(default=10, ge=1)
    min_outcome_coverage: float = Field(default=0.8, ge=0, le=1)
    min_success_rate: float = Field(default=0.0, ge=0, le=1)
    max_success_rate_drop: float = Field(default=0.05, ge=0, le=1)
    max_cost_per_outcome_increase: float = Field(default=0.1, ge=0)
    allow_estimated_cost: bool = False
    allow_inferred_outcomes: bool = False


class VersionComparisonRequest(BaseModel):
    """Request an evidence-gated comparison of two exact workflow versions."""

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)


class DecisionScheduleRequest(BaseModel):
    """Create a recurring economic comparison for two workflow versions."""

    workflow: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    outcome_type: str = Field(default="accepted", min_length=1)
    policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    interval_minutes: int = Field(default=1440, ge=60, le=43_200)


class MigrationObservation(BaseModel):
    """One paired incumbent or candidate outcome used by the scenario engine."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=128)
    cohort: str = Field(default="default", min_length=1, max_length=128)
    cost_usd: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    accepted: bool
    critical_error: bool = False
    source: str = Field(min_length=1, max_length=64)

    @model_serializer(mode="wrap")
    def _preserve_unmeasured_critical_error(self, handler):
        values = handler(self)
        if "critical_error" not in self.model_fields_set:
            values.pop("critical_error", None)
        return values


class MetricForecastReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    calibration_state: Literal["unknown", "calibrated", "warning", "critical"]
    drift_state: Literal["unknown", "stable", "warning", "critical"]
    interval_coverage: float | None = Field(default=None, ge=0, le=1)
    relative_bias: float | None = None
    relative_residual_shift: float | None = Field(default=None, ge=0)
    calibration_periods: int = Field(ge=0)
    assessed_at: datetime | None = None


class ForecastReadiness(BaseModel):
    """Calibration and drift status attached to a forecast evidence snapshot."""

    model_config = ConfigDict(extra="forbid")

    calibration_state: Literal["unknown", "calibrated", "warning", "critical"] = "unknown"
    drift_state: Literal["unknown", "stable", "warning", "critical"] = "unknown"
    interval_coverage: float | None = Field(default=None, ge=0, le=1)
    relative_bias: float | None = None
    relative_residual_shift: float | None = Field(default=None, ge=0)
    calibration_periods: int = Field(default=0, ge=0)
    assessed_at: datetime | None = None
    metrics: list[MetricForecastReadiness] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)


class ForecastCalibrationObservation(BaseModel):
    """One forecast-versus-observed period used for server-side calibration."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    forecast_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    predicted_mean: float
    predicted_low: float
    predicted_high: float
    observed: float
    observed_at: datetime

    @model_validator(mode="after")
    def _interval_is_ordered(self) -> ForecastCalibrationObservation:
        if self.predicted_low > self.predicted_high:
            raise ValueError("forecast interval endpoints must be ordered")
        return self


class MigrationEvidence(BaseModel):
    """Paired model evidence and observed demand periods for one workload."""

    model_config = ConfigDict(extra="forbid")

    workload: str = Field(min_length=1)
    incumbent_model: str = Field(min_length=1)
    candidate_model: str = Field(min_length=1)
    incumbent: list[MigrationObservation] = Field(min_length=1, max_length=5_000)
    candidate: list[MigrationObservation] = Field(min_length=1, max_length=5_000)
    period_request_counts: list[int] = Field(min_length=1, max_length=366)
    demand_horizon: Literal["month", "unknown"] = "unknown"
    readiness: ForecastReadiness = Field(default_factory=ForecastReadiness)

    @model_validator(mode="after")
    def _evidence_is_identified(self) -> MigrationEvidence:
        if any(value <= 0 for value in self.period_request_counts):
            raise ValueError("period_request_counts must be positive")
        for label, observations in (
            ("incumbent", self.incumbent),
            ("candidate", self.candidate),
        ):
            case_ids = [observation.case_id for observation in observations]
            if len(case_ids) != len(set(case_ids)):
                raise ValueError(f"{label} case_id values must be unique")
        incumbent_cohorts = {row.case_id: row.cohort for row in self.incumbent}
        candidate_cohorts = {row.case_id: row.cohort for row in self.candidate}
        if any(
            incumbent_cohorts[case_id] != candidate_cohorts[case_id]
            for case_id in incumbent_cohorts.keys() & candidate_cohorts.keys()
        ):
            raise ValueError("paired case cohorts must match")
        return self


class CohortRoutingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=128)
    cohort_candidate_shares: dict[str, float] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _shares_are_bounded(self) -> CohortRoutingAction:
        if any(
            not cohort or not isfinite(share) or share < 0 or share > 1
            for cohort, share in self.cohort_candidate_shares.items()
        ):
            raise ValueError("cohort candidate shares must be between 0 and 1")
        if not any(self.cohort_candidate_shares.values()):
            raise ValueError("routing action must send some traffic to the candidate")
        return self


class MigrationRiskPolicy(BaseModel):
    """Customer-owned chance constraints and CVaR limit for model migration."""

    model_config = ConfigDict(extra="forbid")

    min_paired_cases: int = Field(default=30, ge=1)
    candidate_shares: list[float] = Field(
        default_factory=lambda: [0.1, 0.25, 0.5, 1.0], max_length=8
    )
    routing_actions: list[CohortRoutingAction] = Field(default_factory=list, max_length=8)
    max_quality_drop: float = Field(default=0.01, ge=0, le=1)
    max_p95_latency_ms: int = Field(default=2_000, ge=0)
    max_critical_error_rate: float = Field(default=0.005, gt=0, le=1)
    max_constraint_breach_probability: float = Field(default=0.05, ge=0, le=1)
    cvar_confidence: float = Field(default=0.95, gt=0, lt=1)
    max_cvar_loss_usd: Decimal = Field(default=Decimal("0"))
    critical_error_penalty_usd: Decimal = Field(default=Decimal("0"), ge=0)
    require_calibrated_forecast: bool = True
    allow_drift_warning: bool = False

    @model_validator(mode="after")
    def _shares_are_ordered_and_bounded(self) -> MigrationRiskPolicy:
        if not self.candidate_shares:
            raise ValueError("candidate_shares must not be empty")
        if any(not isfinite(share) or share <= 0 or share > 1 for share in self.candidate_shares):
            raise ValueError("candidate_shares must be greater than 0 and at most 1")
        if self.candidate_shares != sorted(set(self.candidate_shares)):
            raise ValueError("candidate_shares must be unique and increasing")
        return self


class ProbabilisticMigrationRequest(BaseModel):
    """Request a retained Monte Carlo model-migration recommendation."""

    model_config = ConfigDict(extra="forbid")

    evidence: MigrationEvidence
    policy: MigrationRiskPolicy
    calibration_observations: list[ForecastCalibrationObservation] = Field(
        default_factory=list, max_length=200
    )
    simulations: int = Field(default=10_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class MigrationEvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    outcome_type: str = Field(default="accepted", min_length=1, max_length=64)
    lookback_days: int = Field(default=30, ge=1, le=366)
    cohort_dimension: str = Field(default="cohort", min_length=1, max_length=128)


class MigrationEvidenceRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_source: MigrationEvidenceSource
    policy: MigrationRiskPolicy
    simulations: int = Field(default=10_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class ProbabilisticDecisionScheduleRequest(MigrationEvidenceRefreshRequest):
    interval_minutes: int = Field(default=1440, ge=60, le=43_200)


class RandomizedRolloutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=40)
    candidate_probability: float = Field(default=0.5, gt=0, lt=1)
    cohort_candidate_probabilities: dict[str, float] = Field(default_factory=dict)
    minimum_per_arm: int = Field(default=100, ge=20, le=10_000)


class RandomizedRolloutAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=192)
    cohort: str = Field(default="default", min_length=1, max_length=128)


class RandomizedRolloutVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_type: str = Field(default="accepted", min_length=1, max_length=64)
    bootstrap_samples: int = Field(default=2_000, ge=100, le=10_000)
    seed: int = Field(default=7, ge=0, le=2_147_483_647)


class DecisionReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionReportDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients: list[str] = Field(min_length=1, max_length=20)
    delivery_mode: Literal["attachment", "link"] = "link"

    @model_validator(mode="after")
    def _recipients_look_like_email_addresses(self) -> DecisionReportDeliveryRequest:
        if any(
            not recipient.strip()
            or "@" not in recipient
            or recipient.startswith("@")
            or recipient.endswith("@")
            for recipient in self.recipients
        ):
            raise ValueError("recipients must contain email addresses")
        return self

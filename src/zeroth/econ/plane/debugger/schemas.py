from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class OutcomeDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_version: str
    outcome_type: str
    operator: Literal["equals", "not_equals", "greater_than_or_equal", "less_than_or_equal"]
    target: bool | float | str

    @model_validator(mode="after")
    def validate_target(self) -> "OutcomeDefinitionCreate":
        if self.operator in {"greater_than_or_equal", "less_than_or_equal"} and (
            isinstance(self.target, bool) or not isinstance(self.target, float | int)
        ):
            raise ValueError("ordered outcome predicates require a numeric target")
        if not self.workflow_id.strip() or not self.workflow_version.strip():
            raise ValueError("workflow_id and workflow_version must be non-empty")
        if not self.outcome_type.strip():
            raise ValueError("outcome_type must be non-empty")
        return self


class OutcomeDefinitionOut(OutcomeDefinitionCreate):
    definition_digest: str
    created_at: datetime


class TimelinePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: datetime
    workflow_id: str
    workflow_version: str
    runs: int
    successful_runs: int
    failed_runs: int
    measured_cost_usd: float
    estimated_cost_usd: float
    measured_failure_exposure_usd: float
    estimated_failure_exposure_usd: float
    measured_cost_per_successful_outcome_usd: float | None
    estimated_cost_per_successful_outcome_usd: float | None
    incomplete_events: int


class CohortPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cohort: str
    runs: int
    successful_runs: int
    failed_runs: int
    measured_cost_usd: float
    estimated_cost_usd: float
    measured_cost_per_successful_outcome_usd: float | None
    estimated_cost_per_successful_outcome_usd: float | None
    incomplete_events: int


class BreakagePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_version: str
    step_id: str
    failed_runs: int
    measured_failure_exposure_usd: float
    estimated_failure_exposure_usd: float
    measured_repeated_attempt_cost_usd: float
    estimated_repeated_attempt_cost_usd: float
    attribution: str = "failed_run_exposure_not_step_causality"


class DiagnosticAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "repair_evidence",
        "define_outcome_success",
        "instrument_outcomes",
        "investigate_retry_policy",
        "inspect_failed_runs",
        "retain_current_configuration",
    ]
    rationale: str
    supported_claim: str


class EconomicDiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    window_start: datetime | None
    window_end: datetime | None
    cohort_dimension: str | None
    claim_scope: Literal["observed_economic_exposure"] = "observed_economic_exposure"
    decision_state: Literal[
        "insufficient_evidence", "economic_risk_observed", "stable_observation"
    ]
    data_quality: Literal[
        "incomplete", "estimated_only", "measured_only", "mixed_cost_evidence"
    ]
    event_count: int
    runs: int
    successful_runs: int
    failed_runs: int
    unresolved_runs: int
    undefined_outcome_versions: list[str]
    outcome_coverage: float
    measured_events: int
    estimated_events: int
    unmeasured_events: int
    incomplete_events: int
    measured_cost_usd: float
    estimated_cost_usd: float
    measured_failure_exposure_usd: float
    estimated_failure_exposure_usd: float
    measured_cost_per_successful_outcome_usd: float | None
    estimated_cost_per_successful_outcome_usd: float | None
    top_failure_exposure: BreakagePoint | None
    highest_failure_rate_cohort: CohortPoint | None
    recommended_action: DiagnosticAction
    limitations: list[str]

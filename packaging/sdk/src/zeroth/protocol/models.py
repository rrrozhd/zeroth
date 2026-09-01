"""Transport-safe economic workflow contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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

"""Server-side mirror of the lean SDK's transport contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from zeroth.econ.cost_ownership import CostRole, validate_cost_ownership
from zeroth.econ.outcome_maturity import OutcomeMaturity, validate_outcome_maturity


class SdkExecutionEvent(BaseModel):
    workflow: str = Field(min_length=1)
    workflow_version: str = Field(default="unversioned", min_length=1)
    run_id: str = Field(min_length=1)
    step: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    source_window_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_role: CostRole = "legacy_unknown"
    charge_id: str | None = Field(default=None, min_length=1, max_length=128)
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_version: str = Field(default="unknown", min_length=1)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_measurement: Literal["measured", "estimated", "unmeasured"] = "unmeasured"
    latency_ms: int = Field(default=0, ge=0)
    subject_id: str | None = None
    dimensions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cost_matches_measurement(self) -> SdkExecutionEvent:
        if self.source_window_id is not None:
            self.source_window_id.encode("utf-8")
            if not 1 <= len(self.run_id) <= 128:
                raise ValueError("windowed executions require a run_id of 1–128 characters")
            if self.event_id is None:
                raise ValueError("windowed executions require an explicit event_id")
            if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
                raise ValueError("windowed executions require an aware recorded_at")
            self.recorded_at = self.recorded_at.astimezone(UTC)
        validate_cost_ownership(self.cost_role, self.charge_id, (self.cost_usd,))
        # Preserve explicit-amount callers without inventing an omitted amount.
        if "cost_measurement" not in self.model_fields_set and self.cost_usd is not None:
            self.cost_measurement = "measured"
        if self.cost_measurement == "unmeasured" and self.cost_usd is not None:
            raise ValueError("unmeasured cost must not include a value")
        if self.cost_measurement != "unmeasured" and self.cost_usd is None:
            raise ValueError("measured or estimated cost requires a value")
        return self


class SdkOutcomeEvent(BaseModel):
    workflow: str = Field(min_length=1)
    workflow_version: str = Field(default="unversioned", min_length=1)
    run_id: str = Field(min_length=1)
    accepted: bool | None = None
    maturity: OutcomeMaturity = "unknown"
    outcome_type: str = Field(default="accepted", min_length=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: Literal["measured", "inferred", "mixed"] = "measured"
    value_usd: Decimal | None = Field(default=None, ge=0)
    score: float | None = Field(default=None, ge=0, le=1)
    subject_id: str | None = None
    dimensions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _maturity_contract(self) -> SdkOutcomeEvent:
        validate_outcome_maturity(self.maturity, self.accepted, self.occurred_at)
        if self.maturity != "unknown":
            self.occurred_at = self.occurred_at.astimezone(UTC)
        return self

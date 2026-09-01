"""Server-side mirror of the lean SDK's transport contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SdkExecutionEvent(BaseModel):
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
    def _cost_matches_measurement(self) -> SdkExecutionEvent:
        if self.cost_measurement == "unmeasured" and self.cost_usd is not None:
            raise ValueError("unmeasured cost must not include a value")
        if self.cost_measurement != "unmeasured" and self.cost_usd is None:
            raise ValueError("measured or estimated cost requires a value")
        return self


class SdkOutcomeEvent(BaseModel):
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

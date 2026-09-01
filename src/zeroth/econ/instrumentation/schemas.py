from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Union
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from zeroth.econ.measurement import MeasurementState

DimensionValue = Union[StrictStr, StrictInt, StrictFloat, StrictBool]


def validate_dimensions(value: dict[str, DimensionValue]) -> dict[str, DimensionValue]:
    """Keep analytic dimensions typed, bounded, and safe to index."""
    if len(value) > 16:
        raise ValueError("dimensions may contain at most 16 entries")
    for key, item in value.items():
        if not key or len(key) > 64 or not key.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise ValueError("dimension keys must be 1-64 letters, digits, '.', '_' or '-'")
        if isinstance(item, str) and len(item) > 256:
            raise ValueError("dimension string values may contain at most 256 characters")
    return value


class ExecutionEvent(BaseModel):
    execution_id: str = Field(default_factory=lambda: f"exec_{uuid4().hex}")
    join_key: str | None = None
    tenant_id: str | None = None
    campaign_id: str | None = None
    operation_id: str | None = None
    deployment_ref: str | None = None
    evidence_kind: Literal["production", "synthetic_control", "legacy_unknown"] = "production"
    provider_request_id: str | None = None
    cleanup_status: str | None = None
    workflow_id: str | None = None
    workflow_version: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=1000)
    subject_id: str | None = None
    dimensions: dict[str, DimensionValue] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    capability_id: str
    implementation_id: str
    model_version: str = "unknown"
    token_cost_usd: Decimal | None = None
    tool_cost_usd: Decimal | None = None
    compute_cost_usd: Decimal | None = None
    cost_measurement: MeasurementState | None = None
    usage_measurement: MeasurementState = MeasurementState.UNMEASURED
    latency_ms: int = 0
    compute_time_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    _bounded_dimensions = field_validator("dimensions")(validate_dimensions)

    @model_validator(mode="after")
    def _measurement_values_agree(self) -> ExecutionEvent:
        costs = (self.token_cost_usd, self.tool_cost_usd, self.compute_cost_usd)
        if self.cost_measurement is None:
            self.cost_measurement = (
                MeasurementState.MEASURED
                if any(value is not None for value in costs)
                else MeasurementState.UNMEASURED
            )
        if self.cost_measurement is MeasurementState.UNMEASURED and any(
            value is not None for value in costs
        ):
            raise ValueError("unmeasured cost values must be absent")
        if self.cost_measurement is not MeasurementState.UNMEASURED and all(
            value is None for value in costs
        ):
            raise ValueError("measured or estimated cost requires a value")
        return self


_execution_event_signature = inspect.signature(ExecutionEvent)
ExecutionEvent.__signature__ = _execution_event_signature.replace(
    parameters=[
        parameter.replace(annotation=Decimal, default=Decimal("0"))
        if name in {"token_cost_usd", "tool_cost_usd", "compute_cost_usd"}
        else parameter
        for name, parameter in _execution_event_signature.parameters.items()
        if name
        not in {
            "campaign_id",
            "operation_id",
            "deployment_ref",
            "evidence_kind",
            "provider_request_id",
            "cleanup_status",
            "cost_measurement",
            "usage_measurement",
            "workflow_id",
            "workflow_version",
            "run_id",
            "step_id",
            "attempt",
            "subject_id",
            "dimensions",
        }
    ]
)


class OutcomeEvent(BaseModel):
    execution_id: str
    join_key: str | None = None
    capability_id: str
    outcome_type: Literal["conversion", "fraud_flag", "approval", "custom"]
    outcome_value: Union[float, bool, str]
    outcome_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

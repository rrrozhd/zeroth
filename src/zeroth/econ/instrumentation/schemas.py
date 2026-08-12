from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from zeroth.econ.measurement import MeasurementState


class ExecutionEvent(BaseModel):
    execution_id: str = Field(default_factory=lambda: f"exec_{uuid4().hex}")
    join_key: str | None = None
    tenant_id: str | None = None
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
        if name not in {"cost_measurement", "usage_measurement"}
    ]
)


class OutcomeEvent(BaseModel):
    execution_id: str
    join_key: str | None = None
    capability_id: str
    outcome_type: Literal["conversion", "fraud_flag", "approval", "custom"]
    outcome_value: Union[float, bool, str]
    outcome_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

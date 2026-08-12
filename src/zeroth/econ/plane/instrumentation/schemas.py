from __future__ import annotations

import inspect
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

from zeroth.econ.measurement import MeasurementState


class ExecutionEventCreate(BaseModel):
    tenant_id: str | None = None
    execution_id: str
    join_key: str | None = None
    timestamp: datetime
    capability_id: str
    implementation_id: str
    model_version: str
    token_cost_usd: Decimal | None = None
    tool_cost_usd: Decimal | None = None
    compute_cost_usd: Decimal | None = None
    cost_measurement: MeasurementState | None = None
    usage_measurement: MeasurementState = MeasurementState.UNMEASURED
    latency_ms: int = 0
    compute_time_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _measurement_values_agree(self) -> ExecutionEventCreate:
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


_execution_event_create_signature = inspect.signature(ExecutionEventCreate)
ExecutionEventCreate.__signature__ = _execution_event_create_signature.replace(
    parameters=[
        parameter.replace(annotation=Decimal, default=Decimal("0"))
        if name in {"token_cost_usd", "tool_cost_usd", "compute_cost_usd"}
        else parameter
        for name, parameter in _execution_event_create_signature.parameters.items()
        if name not in {"cost_measurement", "usage_measurement"}
    ]
)


class OutcomeEventCreate(BaseModel):
    tenant_id: str | None = None
    execution_id: str | None = None
    join_key: str | None = None
    capability_id: str
    implementation_id: str | None = None
    outcome_type: Literal["conversion", "fraud_flag", "approval", "custom", "reopen_rate"]
    outcome_value: Union[float, bool, str] | None = None
    outcome_payload_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    outcome_timestamp: datetime | None = None
    provenance: Literal["MEASURED", "INFERRED", "MIXED"] = "MEASURED"


class OutcomeBatchIngestRequest(BaseModel):
    events: list[OutcomeEventCreate]


class OutcomeQueryResponse(BaseModel):
    id: int
    tenant_id: str
    join_key: str
    capability_id: str
    implementation_id: str | None
    outcome_type: str
    outcome_payload_json: dict[str, Any]
    occurred_at: datetime
    provenance: str

    model_config = {"from_attributes": True}


class IngestResult(BaseModel):
    status: str
    execution_id: str

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from zeroth.econ.instrumentation.schemas import DimensionValue, validate_dimensions
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.cost_ownership import CostRole, validate_cost_ownership, validate_charge_cost_input
from zeroth.econ.outcome_maturity import OutcomeMaturity, validate_outcome_maturity


class ExecutionEventCreate(BaseModel):
    tenant_id: str | None = None
    campaign_id: str | None = None
    operation_id: str | None = None
    deployment_ref: str | None = None
    evidence_kind: Literal["production", "synthetic_control", "legacy_unknown"] = "production"
    provider_request_id: str | None = None
    cleanup_status: str | None = None
    workflow_id: str | None = None
    workflow_version: str | None = None
    source_window_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_role: CostRole = "legacy_unknown"
    charge_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = None
    step_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=1000)
    subject_id: str | None = None
    dimensions: dict[str, DimensionValue] = Field(default_factory=dict)
    execution_id: str
    join_key: str | None = None
    timestamp: datetime
    capability_id: str
    implementation_id: str
    model_version: str
    token_cost_usd: Decimal | None = Field(default=None, max_digits=18, decimal_places=8)
    tool_cost_usd: Decimal | None = Field(default=None, max_digits=18, decimal_places=8)
    compute_cost_usd: Decimal | None = Field(default=None, max_digits=18, decimal_places=8)
    cost_measurement: MeasurementState | None = None
    usage_measurement: MeasurementState = MeasurementState.UNMEASURED
    latency_ms: int = 0
    compute_time_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    _exact_charge_cost = field_validator(
        "token_cost_usd", "tool_cost_usd", "compute_cost_usd", mode="before",
    )(validate_charge_cost_input)

    _bounded_dimensions = field_validator("dimensions")(validate_dimensions)

    @model_validator(mode="after")
    def _measurement_values_agree(self) -> ExecutionEventCreate:
        if self.source_window_id is not None:
            self.source_window_id.encode("utf-8")
            if not 1 <= len(self.execution_id) <= 128:
                raise ValueError("windowed executions require an execution_id of 1–128 characters")
            if not self.run_id or len(self.run_id) > 128:
                raise ValueError("windowed executions require an explicit run_id")
            if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
                raise ValueError("windowed executions require an aware timestamp")
            self.timestamp = self.timestamp.astimezone(UTC)
        costs = (self.token_cost_usd, self.tool_cost_usd, self.compute_cost_usd)
        validate_cost_ownership(self.cost_role, self.charge_id, costs)
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


class OutcomeEventCreate(BaseModel):
    tenant_id: str | None = None
    execution_id: str | None = None
    join_key: str | None = None
    capability_id: str
    implementation_id: str | None = None
    outcome_type: Literal["conversion", "fraud_flag", "approval", "custom", "reopen_rate"]
    outcome_value: Union[float, bool, str] | None = None
    maturity: OutcomeMaturity = "unknown"
    outcome_payload_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    outcome_timestamp: datetime | None = None
    provenance: Literal["MEASURED", "INFERRED", "MIXED"] = "MEASURED"

    @model_validator(mode="after")
    def _maturity_contract(self) -> OutcomeEventCreate:
        value = self.outcome_payload_json.get("value", self.outcome_value)
        timestamp = self.occurred_at or self.outcome_timestamp
        validate_outcome_maturity(self.maturity, value, timestamp)
        if self.maturity != "unknown":
            if self.occurred_at is not None and self.outcome_timestamp is not None:
                validate_outcome_maturity(self.maturity, value, self.outcome_timestamp)
                if self.occurred_at != self.outcome_timestamp:
                    raise ValueError("outcome timestamp aliases must name the same instant")
            if self.occurred_at is not None:
                self.occurred_at = self.occurred_at.astimezone(UTC)
            if self.outcome_timestamp is not None:
                self.outcome_timestamp = self.outcome_timestamp.astimezone(UTC)
        return self


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
    maturity: OutcomeMaturity = "unknown"

    @field_validator("maturity", mode="before")
    @classmethod
    def _legacy_maturity(cls, value):
        return value or "unknown"

    model_config = {"from_attributes": True}


class IngestResult(BaseModel):
    status: str
    execution_id: str
    ingested_at: datetime | None = None

    @field_validator("ingested_at")
    @classmethod
    def _stored_ingestion_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

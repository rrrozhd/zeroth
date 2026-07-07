from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class ExecutionEventCreate(BaseModel):
    tenant_id: str | None = None
    execution_id: str
    join_key: str | None = None
    timestamp: datetime
    capability_id: str
    implementation_id: str
    model_version: str
    token_cost_usd: Decimal = Decimal("0")
    tool_cost_usd: Decimal = Decimal("0")
    compute_cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    compute_time_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


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

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ConnectorType = Literal[
    "langfuse",
    "prometheus",
    "otel_metrics",
    "posthog",
    "kafka",
    "segment",
    "snowplow",
    "clickhouse",
    "bigquery",
    "snowflake",
    "launchdarkly",
    "envoy",
]

OutboxStatus = Literal["PENDING", "PROCESSING", "SENT", "FAILED", "DEAD_LETTER"]


class ConnectorConfigOut(BaseModel):
    id: int
    tenant_id: str
    connector_type: ConnectorType
    enabled: bool
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectorConfigRequest(BaseModel):
    config_json: dict[str, Any] = Field(default_factory=dict)


class ConnectorStatusOut(BaseModel):
    connector_type: ConnectorType
    enabled: bool
    healthy: bool
    message: str


class ConnectorOutboxOut(BaseModel):
    id: int
    tenant_id: str
    event_type: str
    event_key: str
    payload_json: dict[str, Any]
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    processed_at: datetime | None

    model_config = {"from_attributes": True}


class ConnectorSendResult(BaseModel):
    success: bool
    status_code: int | None = None
    response_excerpt: str | None = None


class ConnectorHealthResult(BaseModel):
    healthy: bool
    message: str = "ok"


class ConnectorEventEnvelope(BaseModel):
    event_id: str
    event_type: str
    event_key: str
    occurred_at: datetime
    tenant_id: str
    join_key: str | None = None
    capability_id: str | None = None
    implementation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RetryOutboxResponse(BaseModel):
    id: int
    status: OutboxStatus

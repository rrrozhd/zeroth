"""Additive delivery API models kept outside the frozen legacy schema surface."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowPreflightIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None


class WorkflowPreflightResponse(BaseModel):
    workflow_id: str
    version: int
    ready: bool
    checks: list[str]
    issues: list[WorkflowPreflightIssue]


class LiveProviderVerificationRequest(BaseModel):
    acknowledge_external_call: bool = False
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=30.0)
    max_models: int = Field(default=3, ge=1, le=3)
    campaign_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_cost_usd: Decimal | None = Field(default=None, gt=0)
    run_cap_usd: Decimal | None = Field(default=None, gt=0)


class LiveProviderProbe(BaseModel):
    model: str
    ok: bool
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_code: str | None = None
    operation_id: str | None = None
    cost_event_id: str | None = None
    audit_event_id: str | None = None
    cost_measurement: str | None = None
    estimated_cost_usd: Decimal | None = None
    provider_request_id: str | None = None
    cleanup_status: str | None = None


class LiveProviderVerificationResponse(BaseModel):
    workflow_id: str
    verified: bool
    probes: list[LiveProviderProbe]
    campaign_id: str | None = None
    operation_id: str | None = None


class AuditReadinessResponse(BaseModel):
    """Whether this deployment may make its configured audit-integrity claim."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    state: str
    deployment_mode: str
    signing_required: bool
    signer_available: bool
    consequential_actions: bool
    message: str

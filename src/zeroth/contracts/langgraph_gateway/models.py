"""Stable data contracts shared by the LangGraph gateway components."""

from __future__ import annotations

import inspect
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class GovernanceLevel(StrEnum):
    """Cumulative governance capability supported by evidence."""

    ADMISSION = "admission"
    OBSERVED = "observed"
    ENFORCED = "enforced"


class RouteDisposition(StrEnum):
    """How the gateway handles a classified endpoint."""

    GOVERNED = "governed"
    TRANSPARENT = "transparent"
    UNSUPPORTED = "unsupported"


class EndpointKind(StrEnum):
    """Endpoint families that require distinct gateway processing."""

    HTTP = "http"
    PROTOCOL_COMMAND = "protocol_command"
    PROTOCOL_EVENT_STREAM = "protocol_event_stream"


class CompatibilityStatus(StrEnum):
    """Result of matching an upstream against the tested compatibility matrix."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class GatewayEventStatus(StrEnum):
    """Terminal outcomes emitted by gateway request processing."""

    SUCCESS = "success"
    UPSTREAM_ERROR = "upstream_error"
    CLIENT_DISCONNECT = "client_disconnect"
    GATEWAY_DENIAL = "gateway_denial"
    CANCELLATION = "cancellation"


class GatewayError(BaseModel):
    """Stable envelope for errors originating inside the gateway."""

    code: str
    correlation_id: str
    retryable: bool
    reason: str

    @field_validator("code")
    @classmethod
    def validate_code_namespace(cls, value: str) -> str:
        if not value.startswith("zeroth."):
            raise ValueError("gateway error codes must use the zeroth.* namespace")
        return value


class AdmissionRequest(BaseModel):
    """Policy and budget input for a run-creating operation."""

    tenant_id: str
    principal_id: str
    roles: tuple[str, ...]
    deployment_ref: str
    assistant_id: str | None = None
    thread_id: str | None = None
    operation: str
    input_payload: Any = Field(exclude=True, repr=False)
    input_classification: str = "unclassified"
    input_size_bytes: int = Field(ge=0)
    policy_bindings: tuple[str, ...] = ()

    def with_classification(self, classification: str) -> AdmissionRequest:
        """Return an updated copy without mutating the admission input."""
        return self.model_copy(update={"input_classification": classification})


class AdmissionDecision(BaseModel):
    """Combined policy and budget admission result."""

    allowed: bool
    policy_version: str
    reason: str | None = None
    budget_spend_usd: float | None = None
    budget_cap_usd: float | None = None
    budget_check_degraded: bool = False


class GatewayCorrelation(BaseModel):
    """Identifiers extracted without retaining request or response content."""

    correlation_id: str
    deployment_ref: str
    tenant_id: str
    principal_id: str
    assistant_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None


class CompatibilityResult(BaseModel):
    """Evidence-backed compatibility status for one upstream deployment."""

    tested_langgraph_versions: tuple[str, ...]
    tested_agent_server_versions: tuple[str, ...]
    detected_agent_server_version: str | None = None
    openapi_fingerprint: str | None = None
    status: CompatibilityStatus
    reason: str | None = None


class RunCapabilityEvidence(BaseModel):
    """Attested evidence used to clamp a run's reported governance level."""

    correlation_id: str
    run_id: str | None = None
    governance_level: GovernanceLevel
    observed_at: datetime
    graph_version: str | None = None
    adapter_version: str | None = None
    inventory_fingerprint: str | None = None
    signature_valid: bool = False
    tool_manifest_complete: bool = False


RunCapabilityEvidence.__signature__ = inspect.signature(RunCapabilityEvidence).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(RunCapabilityEvidence).parameters.items()
        if name not in {"run_id", "adapter_version", "inventory_fingerprint"}
    ]
)


class GatewayEvent(BaseModel):
    """Content-free terminal event emitted for one gateway operation."""

    correlation: GatewayCorrelation
    operation: str
    disposition: RouteDisposition
    governance_level: GovernanceLevel
    status: GatewayEventStatus
    started_at: datetime
    completed_at: datetime
    policy_version: str | None = None
    budget_spend_usd: float | None = None
    budget_cap_usd: float | None = None
    budget_check_degraded: bool = False
    compatibility_fingerprint: str | None = None
    input_sha256: str | None = None
    input_size_bytes: int | None = Field(default=None, ge=0)
    output_sha256: str | None = None
    output_size_bytes: int | None = Field(default=None, ge=0)
    upstream_status_code: int | None = None

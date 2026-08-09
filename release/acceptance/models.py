"""Versioned acceptance contract and evidence report models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_SCENARIOS = (
    "readiness",
    "authentication",
    "rbac",
    "migrations",
    "workflow_lifecycle",
    "deployment",
    "runs",
    "streaming",
    "approvals",
    "audit",
    "artifacts",
    "retention",
    "gateway_http",
    "gateway_websocket",
    "compatibility",
    "executable_unit_failures",
    "restart_recovery",
    "shutdown",
)


class ScenarioStatus(StrEnum):
    """Fail-closed status used by scenarios, cleanup, and the whole report."""

    PASSED = "passed"
    FAILED = "failed"


class AcceptanceStep(BaseModel):
    """One bounded protocol operation and its fixed assertions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["http", "websocket"]
    role: Literal["anonymous", "operator", "reviewer", "admin"]
    path: str
    method: Literal["GET", "POST", "PUT", "DELETE"] | None = None
    payload: Any | None = None
    expected_status: int | None = None
    expected_json: dict[str, Any] = Field(default_factory=dict)
    require_correlation: bool = False
    capture: dict[str, str] = Field(default_factory=dict)
    owned_capture: dict[str, str] = Field(default_factory=dict)
    max_events: int | None = None
    ordered_events: list[str] = Field(default_factory=list)
    resource_id: str | None = None

    @model_validator(mode="after")
    def _protocol_shape(self) -> AcceptanceStep:
        if self.protocol == "http":
            if self.method is None or self.expected_status is None:
                raise ValueError("HTTP steps require method and expected_status")
            if self.max_events is not None or self.ordered_events:
                raise ValueError("HTTP steps cannot declare WebSocket assertions")
        else:
            if self.method is not None or self.expected_status is not None:
                raise ValueError("WebSocket steps cannot declare HTTP assertions")
            if self.max_events is None or not self.ordered_events:
                raise ValueError("WebSocket steps require max_events and ordered_events")
        if self.method == "DELETE" and self.resource_id is None:
            raise ValueError("DELETE steps require a namespace-owned resource_id")
        if self.method == "DELETE" and self.owned_capture:
            raise ValueError("DELETE steps cannot declare owned captures")
        return self


class ScenarioSpec(BaseModel):
    """Ordered operations proving one required capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: list[AcceptanceStep] = Field(min_length=1)


class AcceptanceContract(BaseModel):
    """Fixture-supplied paths with runner-owned, fail-closed invariants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    supported_agent_server_versions: list[str] = Field(min_length=1)
    scenarios: dict[str, ScenarioSpec]
    cleanup: list[AcceptanceStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _required_invariants(self) -> AcceptanceContract:
        missing = set(REQUIRED_SCENARIOS) - set(self.scenarios)
        if missing:
            raise ValueError(f"missing required scenarios: {', '.join(sorted(missing))}")

        counts = [
            step.expected_json.get("tool_execution_count")
            for step in self.scenarios["approvals"].steps
            if "tool_execution_count" in step.expected_json
        ]
        if counts != [0, 1]:
            raise ValueError(
                "approvals must prove zero times before approval and exactly once after"
            )

        restart = self.scenarios["restart_recovery"].steps
        if not any(step.path == "{restart_url}" and step.method == "POST" for step in restart):
            raise ValueError("restart_recovery must invoke {restart_url}")
        anchors = {"run": True, "approval": True, "artifact": True}
        if sum(step.expected_json == anchors for step in restart) < 2:
            raise ValueError(
                "restart_recovery must prove run, approval, and artifact anchors twice"
            )

        shutdown = self.scenarios["shutdown"].steps
        if not any(step.path == "{shutdown_url}" and step.method == "POST" for step in shutdown):
            raise ValueError("shutdown must invoke {shutdown_url}")

        compatibility = self.scenarios["compatibility"].steps
        expected = [step.expected_json for step in compatibility if step.expected_json]
        if not any(
            value.get("status") == "supported"
            and value.get("detected_agent_server") in self.supported_agent_server_versions
            for value in expected
        ):
            raise ValueError("compatibility must pin a supported Agent Server version")
        return self


class StepObservation(BaseModel):
    """Minimal, redacted protocol evidence retained in the final report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["http", "websocket"]
    path: str
    status_code: int | None = None
    correlation_id: str | None = None
    event_count: int | None = None


class ScenarioResult(BaseModel):
    """Result of one required scenario or cleanup operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: ScenarioStatus
    detail: str
    observations: list[StepObservation] = Field(default_factory=list)


class AcceptanceReport(BaseModel):
    """Promotion evidence produced from the deployed target only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: ScenarioStatus
    target_origin: str
    tenant_id: str
    namespace: str
    deployment_ref: str
    candidate_digest: str
    image_identity: dict[str, str]
    observed_compatibility: dict[str, Any] | None = None
    started_at: datetime
    finished_at: datetime
    scenarios: list[ScenarioResult]
    cleanup: list[ScenarioResult]

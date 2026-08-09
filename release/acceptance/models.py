"""Versioned acceptance contract and evidence report models."""

from __future__ import annotations

import json
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


def canonical(value: Any) -> str:
    """Stable key for comparing declared match patterns."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _read_path(value: Any, path: tuple[str, ...]) -> Any:
    """Read a nested key path, returning None rather than raising when absent."""
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _contains_namespace(value: Any) -> bool:
    if isinstance(value, str):
        return "{namespace}" in value
    if isinstance(value, dict):
        return any(_contains_namespace(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_namespace(item) for item in value)
    return False


def _has_ownership_attestation(expected: dict[str, Any]) -> bool:
    namespace = expected.get("namespace")
    name = expected.get("name")
    return namespace == "{namespace}" or (isinstance(name, str) and name.startswith("{namespace}-"))


class ScenarioStatus(StrEnum):
    """Fail-closed status used by scenarios, cleanup, and the whole report."""

    PASSED = "passed"
    FAILED = "failed"


class AcceptanceStep(BaseModel):
    """One bounded protocol operation and its fixed assertions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["http", "websocket", "lifecycle"]
    role: Literal["anonymous", "operator", "reviewer", "admin"]
    path: str = ""
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
    operation: Literal["restart", "shutdown"] | None = None
    poll: bool = False
    count_path: str | None = None
    count_where: dict[str, Any] = Field(default_factory=dict)
    expected_count: int | None = None
    expect_unreachable: bool = False

    @model_validator(mode="after")
    def _protocol_shape(self) -> AcceptanceStep:
        if self.protocol == "lifecycle":
            return self._lifecycle_shape()
        if self.operation is not None:
            raise ValueError("only lifecycle steps declare an operation")
        if not self.path.startswith("/"):
            raise ValueError("protocol steps require an origin-relative path")
        self._transport_shape()
        self._counting_shape()
        return self._capture_shape()

    def _transport_shape(self) -> None:
        if self.expect_unreachable:
            self._unreachable_shape()
            return
        if self.protocol == "http":
            if self.method is None or self.expected_status is None:
                raise ValueError("HTTP steps require method and expected_status")
            if self.max_events is not None or self.ordered_events:
                raise ValueError("HTTP steps cannot declare WebSocket assertions")
            return
        if self.method is not None or self.expected_status is not None:
            raise ValueError("WebSocket steps cannot declare HTTP assertions")
        if self.max_events is None or not self.ordered_events:
            raise ValueError("WebSocket steps require max_events and ordered_events")
        if self.poll:
            raise ValueError("WebSocket steps cannot be polled")

    def _unreachable_shape(self) -> None:
        """A withdrawn candidate refuses connections; it does not answer politely.

        A deployment that has genuinely stopped serving cannot return a status code
        saying so. Asserting a 503 would only ever pass against something still
        running, which is the opposite of what a drain proves.
        """
        if self.protocol != "http":
            raise ValueError("only HTTP steps can expect an unreachable candidate")
        if self.method is None:
            raise ValueError("unreachable steps require a method")
        forbidden = (
            self.expected_status is not None
            or self.expected_json
            or self.capture
            or self.owned_capture
            or self.count_path is not None
            or self.require_correlation
        )
        if forbidden:
            raise ValueError("unreachable steps cannot assert a response")

    def _counting_shape(self) -> None:
        if (self.count_path is None) != (self.expected_count is None):
            raise ValueError("counting steps require both count_path and expected_count")
        if self.count_path is None:
            if self.count_where:
                raise ValueError("count_where requires count_path")
            return
        if self.protocol != "http":
            raise ValueError("only HTTP steps can count response collections")
        if self.expected_count is not None and self.expected_count < 0:
            raise ValueError("expected_count cannot be negative")

    def _lifecycle_shape(self) -> AcceptanceStep:
        """A lifecycle step names a platform operation, never an application route.

        Restarting or draining a deployed candidate is something the platform does to
        the process. Modelling it as an application endpoint would require the product
        to ship a route that restarts itself, which is both a governance liability and
        untestable against a candidate that is genuinely down.
        """
        if self.operation is None:
            raise ValueError("lifecycle steps require an operation")
        disallowed = (
            self.path
            or self.method is not None
            or self.expected_status is not None
            or self.expected_json
            or self.payload is not None
            or self.capture
            or self.owned_capture
            or self.max_events is not None
            or self.ordered_events
            or self.resource_id is not None
            or self.require_correlation
            or self.poll
        )
        if disallowed:
            raise ValueError("lifecycle steps declare only a role and an operation")
        if self.role != "admin":
            raise ValueError("lifecycle steps require the admin role")
        return self

    def _capture_shape(self) -> AcceptanceStep:
        if self.method == "DELETE" and self.resource_id is None:
            raise ValueError("DELETE steps require a namespace-owned resource_id")
        if self.method == "DELETE" and self.owned_capture:
            raise ValueError("DELETE steps cannot declare owned captures")
        if self.owned_capture:
            if self.method not in {"POST", "PUT"}:
                raise ValueError("owned captures require a mutating create step")
            if not _contains_namespace(self.payload):
                raise ValueError("owned captures require a payload containing {namespace}")
            if not _has_ownership_attestation(self.expected_json):
                raise ValueError(
                    "owned captures require an explicit namespace or namespaced-name "
                    "response attestation"
                )
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
        """The contract's paths are supplied; these guarantees are not negotiable.

        Split by concern rather than written as one pass: this function *is* the
        specification of what a deployed candidate has to demonstrate, and an
        auditor cannot hold an undivided version of it in view.
        """
        self._require_scenario_coverage()
        self._require_access_evidence()
        self._require_lifecycle_evidence()
        self._require_gateway_evidence()
        self._require_durability_evidence()
        return self

    def _require(self, name: str, predicate: Any, detail: str) -> None:
        if not any(predicate(step) for step in self.scenarios[name].steps):
            raise ValueError(f"{name} must {detail}")

    def _require_scenario_coverage(self) -> None:
        missing = set(REQUIRED_SCENARIOS) - set(self.scenarios)
        if missing:
            raise ValueError(f"missing required scenarios: {', '.join(sorted(missing))}")
        unknown = set(self.scenarios) - set(REQUIRED_SCENARIOS)
        if unknown:
            raise ValueError(f"unknown scenarios: {', '.join(sorted(unknown))}")

    def _require_access_evidence(self) -> None:
        require = self._require
        # Candidate *image* identity is bound in the report, from the release record;
        # no deployment endpoint serves its own image digest, so demanding one here
        # would only ever be satisfiable by a fixture. What the deployment can attest
        # is which deployment it is serving, and that it considers itself ready.
        require(
            "readiness",
            lambda step: step.expected_json.get("status") == "ok",
            "prove the candidate reports itself ready",
        )
        require(
            "readiness",
            lambda step: step.expected_json.get("deployment_ref") == "{deployment_ref}",
            "bind the serving deployment to configuration",
        )

        require(
            "authentication",
            lambda step: step.role == "anonymous" and step.expected_status == 401,
            "reject an anonymous request",
        )
        require(
            "rbac",
            lambda step: step.role == "reviewer" and step.expected_status == 403,
            "prove a role-specific denial",
        )

    def _require_lifecycle_evidence(self) -> None:
        require = self._require
        for method, status in (("POST", "draft"), ("GET", None), ("POST", "published")):
            require(
                "workflow_lifecycle",
                lambda step, method=method, status=status: (
                    step.method == method
                    and (status is None or step.expected_json.get("status") == status)
                ),
                f"include {method} lifecycle evidence",
            )
        require(
            "deployment",
            lambda step: step.expected_json.get("deployment_ref") == "{deployment_ref}",
            "prove the serving deployment reference",
        )
        require(
            "runs",
            lambda step: step.method == "POST" and step.expected_status == 202,
            "submit a run",
        )
        # The tenant in the report has to be something the deployment said, not a copy
        # of what the harness was configured with. Zeroth scopes by credential and
        # ignores the acceptance headers, so a report can otherwise name a tenant the
        # run never touched.
        require(
            "runs",
            lambda step: step.expected_json.get("tenant_id") == "{tenant_id}",
            "observe the serving tenant back from the deployment",
        )
        # A deployed run settles asynchronously. A single unpolled GET would pass or
        # fail on timing rather than on behaviour.
        require(
            "runs",
            lambda step: step.method == "GET" and step.poll and "status" in step.expected_json,
            "observe the submitted run settle to a named status",
        )
        require(
            "retention",
            lambda step: step.expected_json.get("enabled") is True,
            "prove retention enforcement",
        )

    def _require_gateway_evidence(self) -> None:
        """The gateway's wire vocabulary is an error envelope, not a decision field.

        A governed request is either forwarded upstream or refused with a `GatewayError`
        whose `code` is namespaced `zeroth.` — a denial is 403 and an unreachable
        upstream is 502 (`proxy.py`). Nothing on the wire carries a `decision` key, so
        an invariant demanding one can only ever be met by a fixture.

        Approval and resume are run-lifecycle facts, not gateway admission outcomes;
        the `approvals` scenario proves them against the real approval API.
        """
        steps = self.scenarios["gateway_http"].steps
        if not all(step.require_correlation for step in steps):
            raise ValueError("gateway_http must require a correlation id on every step")
        if not any(200 <= (step.expected_status or 0) < 300 for step in steps):
            raise ValueError("gateway_http must prove an admitted request reaches upstream")

        def refuses_with_namespaced_code(status: int) -> bool:
            return any(
                step.expected_status == status
                and str(step.expected_json.get("code", "")).startswith("zeroth.")
                for step in steps
            )

        if not refuses_with_namespaced_code(403):
            raise ValueError("gateway_http must prove a policy denial carries a zeroth.* code")
        if not refuses_with_namespaced_code(502):
            raise ValueError("gateway_http must prove an upstream failure carries a zeroth.* code")

        if not any(
            len(step.ordered_events) > 1 for step in self.scenarios["gateway_websocket"].steps
        ):
            raise ValueError("gateway_websocket must prove causally ordered proxied events")

    def _require_durability_evidence(self) -> None:
        # The claim is about executions, so the evidence has to be a count of records
        # the deployment published — not a field it was asked to report.
        counting = [step for step in self.scenarios["approvals"].steps if step.count_path]
        counts = [step.expected_count for step in counting]
        # Zero may be established more than once — proving it still holds after a
        # restart taken while the approval is pending is the strongest form of this
        # evidence — but the sequence has to end at exactly one.
        if len(counts) < 2 or set(counts[:-1]) != {0} or counts[-1] != 1:
            raise ValueError(
                "approvals must count zero executions before approval and exactly one after"
            )
        if len({(step.count_path, canonical(step.count_where)) for step in counting}) != 1:
            raise ValueError("approvals must count the same records before and after approval")

        restart = self.scenarios["restart_recovery"].steps
        if not any(step.operation == "restart" for step in restart):
            raise ValueError("restart_recovery must invoke the restart lifecycle operation")
        anchors = [
            (step.path, step.count_path, canonical(step.count_where), step.expected_count)
            for step in restart
            if step.count_path
        ]
        if len(anchors) < 2 or len(set(anchors)) != 1:
            raise ValueError(
                "restart_recovery must assert the identical durable fact before and after restart"
            )

        shutdown = self.scenarios["shutdown"].steps
        if not any(step.operation == "shutdown" for step in shutdown):
            raise ValueError("shutdown must invoke the shutdown lifecycle operation")
        readiness_withdrawn = any(
            step.path == "/health/ready"
            and (step.expected_status == 503 or step.expect_unreachable)
            for step in shutdown
        )
        if not readiness_withdrawn:
            raise ValueError("shutdown must prove readiness is withdrawn")

        # The deployment reports Agent Server compatibility through its readiness
        # probe: the `agent_server` dependency check carries the CompatibilityStatus
        # value (`health.py`). There is no endpoint that returns a compatibility
        # document, so an invariant demanding one describes nothing.
        if not any(
            _read_path(step.expected_json, ("checks", "agent_server", "status")) == "supported"
            for step in self.scenarios["compatibility"].steps
        ):
            raise ValueError(
                "compatibility must prove the deployment reports a supported Agent Server"
            )


class StepObservation(BaseModel):
    """Minimal, redacted protocol evidence retained in the final report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["http", "websocket", "lifecycle"]
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

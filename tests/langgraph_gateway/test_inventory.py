import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zeroth.core.langgraph_gateway.inventory import (
    ENDPOINT_RULES,
    classify_endpoint,
    classify_protocol_command,
)
from zeroth.core.langgraph_gateway.models import (
    AdmissionDecision,
    AdmissionRequest,
    CompatibilityResult,
    CompatibilityStatus,
    EndpointKind,
    GatewayCorrelation,
    GatewayError,
    GatewayEvent,
    GatewayEventStatus,
    GovernanceLevel,
    RouteDisposition,
    RunCapabilityEvidence,
)


GOVERNED = [
    ("POST", "/threads/t/runs"),
    ("POST", "/threads/t/runs/stream"),
    ("POST", "/threads/t/runs/wait"),
    ("POST", "/runs/stream"),
    ("POST", "/runs/wait"),
]

TRANSPARENT = [
    ("GET", "/ok"),
    ("GET", "/info"),
    ("GET", "/openapi.json"),
    ("POST", "/assistants"),
    ("GET", "/assistants/a"),
    ("GET", "/assistants/a/graph"),
    ("POST", "/assistants/search"),
    ("POST", "/threads"),
    ("GET", "/threads/t"),
    ("POST", "/threads/search"),
    ("GET", "/threads/t/stream"),
    ("GET", "/threads/t/state"),
    ("POST", "/threads/t/state"),
    ("POST", "/threads/t/state/checkpoint"),
    ("POST", "/threads/t/history"),
    ("GET", "/threads/t/runs/r"),
    ("GET", "/threads/t/runs/r/join"),
    ("GET", "/threads/t/runs/r/stream"),
    ("POST", "/threads/t/runs/r/cancel"),
    ("POST", "/threads/t/stream/events"),
    ("WS", "/threads/t/stream/events"),
]

UNSUPPORTED = [
    ("POST", "/runs"),
    ("POST", "/runs/crons"),
    ("POST", "/a2a/a"),
    ("GET", "/mcp/"),
    ("GET", "/store/items"),
    ("POST", "/custom"),
    ("DELETE", "/threads/t"),
]


@pytest.mark.parametrize(("method", "path"), GOVERNED)
def test_governed_endpoint_inventory(method, path):
    rule = classify_endpoint(method, path)

    assert rule is not None
    assert rule.disposition is RouteDisposition.GOVERNED


@pytest.mark.parametrize(("method", "path"), TRANSPARENT)
def test_transparent_endpoint_inventory(method, path):
    rule = classify_endpoint(method, path)

    assert rule is not None
    assert rule.disposition is RouteDisposition.TRANSPARENT


@pytest.mark.parametrize(("method", "path"), UNSUPPORTED)
def test_routes_outside_the_claim_are_unsupported(method, path):
    assert classify_endpoint(method, path) is None


def test_protocol_command_endpoint_is_body_dependent():
    rule = classify_endpoint("POST", "/threads/t/commands")

    assert rule is not None
    assert rule.kind is EndpointKind.PROTOCOL_COMMAND
    assert rule.disposition is None


@pytest.mark.parametrize("method", ["run.start", "input.respond"])
def test_run_creating_protocol_commands_are_governed(method):
    assert classify_protocol_command({"id": 1, "method": method, "params": {}}) is (
        RouteDisposition.GOVERNED
    )


@pytest.mark.parametrize(
    "method",
    ["agent.getTree", "subscription.subscribe", "subscription.unsubscribe"],
)
def test_known_non_run_protocol_commands_are_transparent(method):
    assert classify_protocol_command({"id": 1, "method": method}) is (RouteDisposition.TRANSPARENT)


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"method": ""}, {"method": "run.delete"}, {"method": 7}],
)
def test_unknown_or_malformed_protocol_commands_are_unsupported(payload):
    assert classify_protocol_command(payload) is RouteDisposition.UNSUPPORTED


def test_rules_are_immutable_and_patterns_are_anchored():
    rule = ENDPOINT_RULES[0]

    assert rule.path_pattern.pattern.startswith("^")
    assert rule.path_pattern.pattern.endswith("$")
    with pytest.raises(FrozenInstanceError):
        rule.method = "DELETE"


def test_claimed_http_inventory_matches_pinned_openapi_projection():
    fixture = Path(__file__).parent / "fixtures" / "openapi-0.11.1.operations.json"
    projection = json.loads(fixture.read_text(encoding="utf-8"))
    projected_routes = {(method, path) for method, path, _ in projection["operations"]}
    claimed_routes = {(rule.method, rule.path_template) for rule in ENDPOINT_RULES}

    # An OpenAPI document conventionally does not list its own serving route.
    # WebSocket is the other explicit exception because OpenAPI cannot express it.
    projection_exceptions = {
        ("GET", "/openapi.json"),
        ("WS", "/threads/{thread_id}/stream/events"),
    }
    assert projection_exceptions <= claimed_routes
    assert claimed_routes - projection_exceptions <= projected_routes
    assert projection["package_version"] == "0.11.1"


def test_gateway_error_requires_a_namespaced_code():
    with pytest.raises(ValueError):
        GatewayError(code="denied", correlation_id="corr-1", retryable=False, reason="denied")


def test_admission_request_never_serializes_or_represents_input_payload():
    request = AdmissionRequest(
        tenant_id="tenant-a",
        principal_id="user-1",
        roles=("operator",),
        deployment_ref="external-agent",
        operation="runs.create",
        input_payload={"secret": "must-not-leak"},
        input_size_bytes=26,
    )

    assert "input_payload" not in request.model_dump()
    assert "must-not-leak" not in repr(request)
    classified = request.with_classification("restricted")
    assert classified.input_classification == "restricted"
    assert request.input_classification == "unclassified"


def test_admission_decision_carries_budget_degradation_without_weakening_denial():
    decision = AdmissionDecision(
        allowed=False,
        policy_version="sha256:abc",
        reason="budget denied",
        budget_spend_usd=10.0,
        budget_cap_usd=5.0,
        budget_check_degraded=True,
    )

    assert decision.allowed is False
    assert decision.budget_check_degraded is True


def test_compatibility_correlation_capability_and_event_shapes_compose():
    now = datetime.now(UTC)
    compatibility = CompatibilityResult(
        tested_langgraph_versions=("1.2.9",),
        tested_agent_server_versions=("0.11.1",),
        detected_agent_server_version="0.11.1",
        openapi_fingerprint="sha256:abc",
        status=CompatibilityStatus.SUPPORTED,
    )
    correlation = GatewayCorrelation(
        correlation_id="corr-1",
        deployment_ref="external-agent",
        tenant_id="tenant-a",
        principal_id="user-1",
    )
    evidence = RunCapabilityEvidence(
        correlation_id="corr-1",
        governance_level=GovernanceLevel.ADMISSION,
        observed_at=now,
    )
    event = GatewayEvent(
        correlation=correlation,
        operation="runs.create",
        disposition=RouteDisposition.GOVERNED,
        governance_level=evidence.governance_level,
        status=GatewayEventStatus.SUCCESS,
        started_at=now,
        completed_at=now,
        compatibility_fingerprint=compatibility.openapi_fingerprint,
    )

    assert event.correlation.correlation_id == evidence.correlation_id
    assert event.compatibility_fingerprint == "sha256:abc"

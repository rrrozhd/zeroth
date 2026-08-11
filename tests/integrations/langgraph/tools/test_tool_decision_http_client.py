"""The HTTP-backed tool decision client (ZER-8 S10).

``_tool_decisions.resolve_tool_decision`` already turns every way a client can
fail into a denial. That makes the *client's* job narrow and unusual: it must
not be defensive. Any attempt to be helpful here -- retrying past its timeout,
returning a default verdict when the service is unreachable, treating a 503 as
"no opinion" -- would manufacture an answer the policy source never gave, and
the seam above would faithfully pass it through as if it had.

So these tests are mostly about what the client refuses to do:

* a transport failure **propagates** rather than becoming a fabricated verdict,
  and the seam above converts it to a denial (R6);
* an error status is never read as an allow, whatever its body says;
* a body that is not a decision is not coerced into one.

The allow path is tested too, because without it every assertion above could
pass for the trivial reason that the client never allows anything at all.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from zeroth.integrations.langgraph._tool_decision_http import (
    DEFAULT_DECISION_TIMEOUT_SECONDS,
    HttpToolDecisionClient,
)
from zeroth.integrations.langgraph._tool_decisions import (
    UnknownSideEffectPolicy,
    resolve_tool_decision,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolIdentity,
    ToolPolicyDescriptor,
    describe_tool_policy,
)
from zeroth.integrations.langgraph.enforcement_protocol import ActionDescriptorV1
from zeroth.governance.decisions.request import NormalizedAction

BASE_URL = "http://zeroth.test"
DEPLOYMENT = "dep-1"


def _action() -> ToolAction:
    return ToolAction(
        identity=ToolIdentity(name="search", fingerprint="sha256:tool"),
        arguments={"query": "weather"},
        principal_id="principal-1",
        side_effect=SideEffectClass.READ_ONLY,
        capability_refs=("network_read",),
        requires_approval=True,
    )


def _context() -> ToolGovernanceContext:
    return ToolGovernanceContext(
        tenant_id="tenant-a",
        principal_id="principal-1",
        run_id="run-1",
        thread_id="thread-1",
        correlation_id="corr-1",
    )


def _decision_body(kind: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "schema_version": 1,
        "decision_id": "decision-1",
        "kind": kind,
        "reason_code": "policy_violation",
        "approval_ref": None,
        "policy_version": "policy-7",
        "tenant_id": "tenant-a",
        "issued_at": "2026-07-30T12:00:00Z",
    }
    body.update(overrides)
    return body


def _client(handler: Any, **kwargs: Any) -> HttpToolDecisionClient:
    """Build a client whose transport is the given handler."""
    transport = httpx.MockTransport(handler)
    return HttpToolDecisionClient(
        base_url=BASE_URL,
        deployment_ref=DEPLOYMENT,
        api_key="secret",
        client=httpx.Client(transport=transport, base_url=BASE_URL),
        **kwargs,
    )


def test_an_allow_verdict_is_returned_as_the_seam_type() -> None:
    """The positive control: without it every refusal test below is vacuous."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_decision_body("allow", reason_code="unknown_error"))

    decision = _client(handler).decide(_action(), _context())

    assert decision.kind is ToolDecisionKind.ALLOW
    assert captured["url"].endswith("/v1/enforcement/decisions")
    assert captured["headers"]["x-api-key"] == "secret"
    # The arguments themselves are never carried -- only their digest.
    assert "weather" not in json.dumps(captured["body"])
    assert captured["body"]["action"]["fingerprint"] == "sha256:tool"
    assert captured["body"]["action"]["capability_refs"] == ["network_read"]
    assert captured["body"]["action"]["requires_approval"] is True
    assert captured["body"]["deployment_ref"] == DEPLOYMENT


def test_both_decision_wires_accept_the_whole_canonical_policy_descriptor() -> None:
    """A new policy field cannot be added to the source record and dropped by a wire."""
    descriptor = describe_tool_policy(_action())
    expected = ToolPolicyDescriptor(
        identity=ToolIdentity(name="search", fingerprint="sha256:tool"),
        side_effect=SideEffectClass.READ_ONLY,
        contract_ref=None,
        capability_refs=("network_read",),
        requires_approval=True,
    )
    assert descriptor == expected
    policy_fields = descriptor.wire_fields()

    normalized = NormalizedAction(**policy_fields, arguments_digest="sha256:arguments")
    gateway = ActionDescriptorV1(**policy_fields, arguments={"query": "weather"})

    assert normalized.model_dump(include=set(policy_fields)) == policy_fields
    assert gateway.model_dump(include=set(policy_fields)) == policy_fields


def test_a_deny_verdict_keeps_the_reason_the_service_gave() -> None:
    """A denial is reported under the service's reason, not a client-side one."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_decision_body("deny", reason_code="policy_violation"))

    decision = _client(handler).decide(_action(), _context())

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_violation"


def test_require_approval_survives_as_its_own_verdict() -> None:
    """An approval is not a denial: collapsing them would lose the distinction."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_decision_body(
                "require_approval",
                reason_code="policy_violation",
                approval_ref="approval-9",
            ),
        )

    decision = _client(handler).decide(_action(), _context())

    assert decision.kind is ToolDecisionKind.REQUIRE_APPROVAL
    assert decision.approval_ref == "approval-9"


def test_a_transport_failure_propagates_rather_than_becoming_a_verdict() -> None:
    """R6, client half: the client must not invent an answer it never received."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable")

    with pytest.raises(httpx.HTTPError):
        _client(handler).decide(_action(), _context())


def test_an_unreachable_service_denies_at_the_seam(monkeypatch) -> None:
    """R6 end to end: the propagated failure becomes a denial, never an allow.

    This is the pairing that matters. The test above proves the client does not
    fabricate; this one proves that not fabricating still fails *closed*, because
    ``resolve_tool_decision`` is what converts the raise into a verdict.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable")

    decision = resolve_tool_decision(
        _action(),
        _context(),
        client=_client(handler),
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
    )

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_unavailable"


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 500, 503])
def test_no_error_status_is_ever_read_as_an_allow(status_code: int) -> None:
    """An error body that happens to contain ``allow`` is still an error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"kind": "allow"})

    with pytest.raises(httpx.HTTPError):
        _client(handler).decide(_action(), _context())


def test_a_body_that_is_not_a_decision_is_not_coerced_into_one() -> None:
    """A 200 carrying nonsense is a failure, not a permissive default."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"kind": "allow"})

    with pytest.raises(ValueError):
        _client(handler).decide(_action(), _context())


def test_two_calls_are_two_attempts_with_distinct_replay_tokens() -> None:
    """An identical repeated call is judged again, not answered from the first.

    The idempotency key scopes one *attempt*, so a transport retry of a single
    attempt cannot double-record -- but a genuinely repeated tool call is a new
    attempt that policy (rate, budget, approval) must be allowed to refuse.
    """
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(json.loads(request.content)["idempotency_key"])
        return httpx.Response(200, json=_decision_body("allow", reason_code="unknown_error"))

    client = _client(handler)
    client.decide(_action(), _context())
    client.decide(_action(), _context())

    assert len(set(keys)) == 2


def test_the_request_timeout_is_bounded_and_overridable() -> None:
    """A decision blocks the caller, so its wait is bounded by construction."""
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json=_decision_body("allow", reason_code="unknown_error"))

    _client(handler).decide(_action(), _context())
    _client(handler, timeout=0.25).decide(_action(), _context())

    assert seen[0]["read"] == DEFAULT_DECISION_TIMEOUT_SECONDS
    assert seen[1]["read"] == 0.25
    assert DEFAULT_DECISION_TIMEOUT_SECONDS <= 30.0

"""Proof that the decision seam fails closed on every path, not just the tidy one.

"Fails closed" is cheap to claim with a client that politely returns DENY. The
cases that matter are the ones where nothing usable comes back at all: no client
injected, a client that raises, a client that returns ``None``, a duck-typed
impostor, a verdict from some other enum, a ``ToolDecision`` subclass. Each of
those has to end in a denial, and each is exercised below.

The other half is the opt-in. An escape hatch that a stray ``True``, a bare
string or a default could trip is not an opt-in, so
:class:`UnknownSideEffectPolicy` is an enum compared by identity, and the tests
push every near-miss at it.

Neither ``langgraph`` nor ``langchain`` is imported, so there is no
``langgraph_conformance`` marker -- ``addopts`` deselects it and a marked test
would never run.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum

import pytest

from tests.integrations.langgraph.tools._hostile import HostileStr
from zeroth.governance.audit.capture_vocabulary import REASON_CODES
from zeroth.integrations.langgraph._tool_decisions import (
    FailClosedToolDecisionClient,
    ToolDecisionClient,
    UnknownSideEffectPolicy,
    resolve_tool_decision,
)
from zeroth.integrations.langgraph._tool_errors import GovernanceContextError
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)

CONTEXT = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

READ_ONLY = normalize_tool_action(
    name="search",
    arguments={"query": "weather"},
    context=CONTEXT,
    side_effect=SideEffectClass.READ_ONLY,
)
UNCLASSIFIED = normalize_tool_action(
    name="mystery",
    arguments={},
    context=CONTEXT,
    side_effect=None,
)

ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
"""An allow, recorded under the only term an allow can currently carry.

``REASON_CODES`` holds failures and denial reasons; no term names "a policy
allowed this", so an allow's reason code always lands on the unspecified
fallback. Minting one is the enforcement stage's to do, along with the approval
term ``REQUIRE_APPROVAL`` has no code for either.
"""


class OtherKind(StrEnum):
    """A verdict enum that is not ``ToolDecisionKind`` but spells ``allow`` the same."""

    ALLOW = "allow"


class NotADecision:
    """A duck-typed impostor with the attributes a verdict has."""

    kind = ToolDecisionKind.ALLOW
    reason_code = "policy_violation"
    approval_ref = None


@dataclasses.dataclass(frozen=True, slots=True)
class DecisionSubclass(ToolDecision):
    """A subclass ``isinstance`` accepts and the exact-type gate does not."""


@dataclasses.dataclass
class StubClient:
    """A client that returns whatever it was handed, and counts being asked."""

    verdict: object = ALLOW
    calls: int = 0

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Record the call and return the configured verdict."""
        self.calls += 1
        return self.verdict  # type: ignore[return-value]


@dataclasses.dataclass
class RaisingClient:
    """A client that fails the way a network-backed one eventually will."""

    error: BaseException = dataclasses.field(default_factory=lambda: RuntimeError("upstream down"))

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Raise instead of answering."""
        raise self.error


def resolve(**overrides) -> ToolDecision:
    """Resolve a verdict for the read-only action, with the given fields overridden."""
    kwargs: dict = {"action": READ_ONLY, "context": CONTEXT}
    kwargs.update(overrides)
    return resolve_tool_decision(**kwargs)


# --- the default is a denial ---------------------------------------------------


def test_the_default_client_denies_every_call_it_is_shown() -> None:
    # Not a placeholder awaiting ZER-8: a runtime with no policy source has not
    # decided that its tools are safe.
    client = FailClosedToolDecisionClient()

    for action in (READ_ONLY, UNCLASSIFIED):
        decision = client.decide(action, CONTEXT)

        assert decision.kind is ToolDecisionKind.DENY


def test_resolving_with_no_client_at_all_denies() -> None:
    decision = resolve()

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_unavailable"


# --- every way a client can fail to answer is a denial -------------------------


@pytest.mark.parametrize(
    "client",
    [
        RaisingClient(),
        RaisingClient(error=TimeoutError("no route")),
        RaisingClient(error=ValueError("garbage response")),
        StubClient(verdict=None),
        StubClient(verdict=NotADecision()),
        StubClient(verdict=OtherKind.ALLOW),
        StubClient(verdict="allow"),
        StubClient(verdict=ToolDecision(kind=OtherKind.ALLOW, reason_code="x")),  # type: ignore[arg-type]
        StubClient(verdict=DecisionSubclass(kind=ToolDecisionKind.ALLOW, reason_code="x")),
    ],
    ids=[
        "raises_runtime",
        "raises_timeout",
        "raises_value",
        "returns_none",
        "duck_typed",
        "foreign_enum",
        "bare_string",
        "foreign_kind_field",
        "decision_subclass",
    ],
)
def test_a_client_that_does_not_return_a_recognized_verdict_denies(client: object) -> None:
    decision = resolve(client=client)

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_unavailable"


def test_a_torn_down_run_is_not_mistaken_for_a_denial() -> None:
    # BaseException is cancellation, not a verdict: swallowing it would turn a
    # shutdown into a governance decision and hide the interrupt.
    with pytest.raises(KeyboardInterrupt):
        resolve(client=RaisingClient(error=KeyboardInterrupt()))


def test_an_unattributable_call_is_undecidable_rather_than_allowed() -> None:
    # Raised, not returned as a denial: there is no principal to record one
    # against. What it must never be is an allow.
    for context in (None, {"principal_id": "attacker"}, dataclasses.replace(CONTEXT, run_id=" ")):
        with pytest.raises(GovernanceContextError):
            resolve(context=context, client=StubClient())


def test_an_action_that_was_never_normalized_is_denied() -> None:
    decision = resolve_tool_decision({"name": "search"}, CONTEXT, StubClient())

    assert decision.kind is ToolDecisionKind.DENY


# --- an unclassified tool is denied, and the opt-in is hard to trip ------------


def test_an_unclassified_tool_is_denied_before_a_client_is_even_asked() -> None:
    client = StubClient()

    decision = resolve_tool_decision(UNCLASSIFIED, CONTEXT, client)

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_violation"
    assert client.calls == 0


@pytest.mark.parametrize(
    "policy",
    [
        True,
        1,
        "allow_unclassified_tools",
        "allow",
        None,
        UnknownSideEffectPolicy.DENY,
        object(),
    ],
    ids=["true", "one", "bare_value", "bare_allow", "none", "deny", "object"],
)
def test_nothing_but_the_named_member_switches_the_opt_in_on(policy: object) -> None:
    # UnknownSideEffectPolicy is a StrEnum, so its members equal their string
    # values; an equality check here would let the bare literal through.
    decision = resolve_tool_decision(
        UNCLASSIFIED,
        CONTEXT,
        StubClient(),
        unknown_side_effect=policy,  # type: ignore[arg-type]
    )

    assert decision.kind is ToolDecisionKind.DENY


def test_the_named_opt_in_hands_an_unclassified_tool_to_the_client() -> None:
    # The one escape, and it only reaches a client -- it does not itself allow.
    client = StubClient()

    decision = resolve_tool_decision(
        UNCLASSIFIED,
        CONTEXT,
        client,
        unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
    )

    assert client.calls == 1
    assert decision.kind is ToolDecisionKind.ALLOW


def test_the_opt_in_does_not_survive_a_client_that_cannot_answer() -> None:
    decision = resolve_tool_decision(
        UNCLASSIFIED,
        CONTEXT,
        RaisingClient(),
        unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
    )

    assert decision.kind is ToolDecisionKind.DENY


def hostile_action(side_effect: object) -> ToolAction:
    """Build an exactly-typed action carrying a classification nobody minted.

    ``resolve_tool_decision`` gates the action with ``type(action) is ToolAction``
    and then trusts its fields, and ``ToolAction`` is a plain dataclass anyone can
    construct: only ``normalize_tool_action`` runs the classification through
    ``classify_side_effect``. So this is the shape a caller that skipped
    normalization -- or a future surface that builds the descriptor itself --
    hands the gate.
    """
    return ToolAction(
        identity=UNCLASSIFIED.identity,
        arguments={},
        contract_ref=None,
        principal_id=CONTEXT.principal_id,
        side_effect=side_effect,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "side_effect",
    [
        "unknown",
        "read_only",
        "side_effecting",
        HostileStr("read_only"),
        None,
        True,
        1,
        object(),
    ],
    ids=[
        "bare_unknown",
        "bare_read_only",
        "bare_side_effecting",
        "hostile_str",
        "none",
        "true",
        "one",
        "object",
    ],
)
def test_a_classification_that_is_not_an_enum_member_is_unknown_and_therefore_denied(
    side_effect: object,
) -> None:
    # ``SideEffectClass`` is a StrEnum, so ``"read_only"`` compares equal to the
    # member and ``"unknown"`` compares equal to UNKNOWN. A ``!= UNKNOWN`` gate
    # therefore reads every value here except ``"unknown"`` as *classified* and
    # hands it to the client, which is an unclassified tool reaching an allow.
    client = StubClient()

    decision = resolve_tool_decision(hostile_action(side_effect), CONTEXT, client)

    assert decision.kind is ToolDecisionKind.DENY
    assert decision.reason_code == "policy_violation"
    assert client.calls == 0


def test_the_named_opt_in_treats_a_non_member_classification_as_the_unclassified_it_is() -> None:
    # The opt-in's meaning is unchanged by the tighter gate: it still admits a
    # tool nobody classified, and an unusable classification *is* nobody having
    # classified it. It reaches a client and never itself allows.
    client = StubClient()

    decision = resolve_tool_decision(
        hostile_action("read_only"),
        CONTEXT,
        client,
        unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
    )

    assert client.calls == 1
    assert decision.kind is ToolDecisionKind.ALLOW


def test_a_real_member_still_classifies_the_tool_and_reaches_the_client() -> None:
    # The positive control for the gate above: tightening it must not turn a
    # genuine classification into an unclassified denial.
    client = StubClient()

    decision = resolve_tool_decision(hostile_action(SideEffectClass.READ_ONLY), CONTEXT, client)

    assert client.calls == 1
    assert decision.kind is ToolDecisionKind.ALLOW


# --- a real verdict passes through, and its reason code is repaired not rewritten


def test_a_recognized_allow_is_the_only_thing_that_allows() -> None:
    decision = resolve(client=StubClient(verdict=ALLOW))

    assert decision.kind is ToolDecisionKind.ALLOW


@pytest.mark.parametrize("kind", list(ToolDecisionKind))
def test_repairing_an_unregistered_reason_code_never_changes_the_verdict(
    kind: ToolDecisionKind,
) -> None:
    # An unregistered code is summarized away at the audit capture boundary, so
    # it is replaced -- but replacing how a decision is *recorded* must not
    # change what was decided.
    verdict = ToolDecision(kind=kind, reason_code="totally_made_up_reason")

    decision = resolve(client=StubClient(verdict=verdict))

    assert decision.kind is kind
    assert decision.reason_code == "unknown_error"


def test_a_registered_reason_code_survives_untouched() -> None:
    verdict = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="budget_exceeded")

    assert resolve(client=StubClient(verdict=verdict)).reason_code == "budget_exceeded"


def test_a_hostile_approval_reference_does_not_survive_the_seam() -> None:
    verdict = ToolDecision(
        kind=ToolDecisionKind.REQUIRE_APPROVAL,
        reason_code="policy_violation",
        approval_ref=HostileStr("approval-1"),
    )

    decision = resolve(client=StubClient(verdict=verdict))

    assert decision.kind is ToolDecisionKind.REQUIRE_APPROVAL
    assert decision.approval_ref is None


@pytest.mark.parametrize(
    "client",
    [None, FailClosedToolDecisionClient(), RaisingClient(), StubClient(verdict=None)],
    ids=["absent", "fail_closed", "raises", "nonsense"],
)
def test_every_reason_code_the_seam_mints_is_registered_for_audit(client: object) -> None:
    # An unregistered code is replaced by a digest at the capture boundary: the
    # denial would still be written and the reason would be gone. That is the
    # ZER-5 failure this assertion exists to keep out.
    for action in (READ_ONLY, UNCLASSIFIED):
        decision = resolve_tool_decision(action, CONTEXT, client)  # type: ignore[arg-type]

        assert decision.reason_code in REASON_CODES


def test_the_protocol_is_satisfied_structurally_by_the_default_client() -> None:
    # The seam is a Protocol, so ZER-8 supplies a client without importing a
    # base class from here.
    client: ToolDecisionClient = FailClosedToolDecisionClient()

    assert client.decide(READ_ONLY, CONTEXT).kind is ToolDecisionKind.DENY

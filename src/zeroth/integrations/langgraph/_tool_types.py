"""The vocabulary a governed LangGraph tool call is described and decided in.

Declarative only. Nothing here normalizes a tool call, classifies a side
effect, or decides anything: the normalization is the wrapper's (T3) and the
enforcement is the middleware's (T4). This module fixes the *nouns* those
stages exchange, so they cannot disagree about what a tool action is.

**Why a local tri-state instead of widening ``PolicyDecision``.**
:class:`~zeroth.governance.policy.models.PolicyDecision` is ALLOW/DENY and every
consumer of an ``EnforcementResult`` branches ``is PolicyDecision.ALLOW`` and
fails the run otherwise, so a third member would turn "this needs a human" into
a hard failure at every one of those call sites. ``EnforcementResult`` is also
``extra="forbid"`` and is serialized whole into audit metadata, so its shape is
a persisted contract. Tool enforcement needs a third verdict --
:attr:`ToolDecisionKind.REQUIRE_APPROVAL` -- so it mints its own enum, local to
this integration, and leaves the platform-wide one alone.

**Why frozen dataclasses rather than pydantic models.** Every peer integration
puts its models in a ``models.py`` that ``tests/architecture/test_library_surface.py``
imports and pins against the *immutable* backend surface fixtures. These types
are public -- ``zeroth.integrations.langgraph`` re-exports them, because the
enforcement path's exact-type gates make them mandatory rather than optional --
but they are published from this private module and stay out of pydantic
entirely, so they never enter a ``models.py`` the fixture would freeze. Public
API and pinned schema surface are two different things; only the second is
irreversible.

Container fields are snapshotted on construction rather than validated: a caller
that keeps a reference to the mapping it passed in cannot mutate the action a
decision was made about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class SideEffectClass(StrEnum):
    """How consequential invoking a tool is, as far as the classifier can tell.

    ``UNKNOWN`` is a real member, not a placeholder: an unclassified tool must
    be distinguishable from one positively known to be read-only, because a
    policy that waves through read-only calls must not wave through the ones
    nobody classified.
    """

    READ_ONLY = "read_only"
    SIDE_EFFECTING = "side_effecting"
    UNKNOWN = "unknown"


class ToolDecisionKind(StrEnum):
    """The verdicts tool governance can return for one attempted call.

    Deliberately *not*
    :class:`~zeroth.governance.policy.models.PolicyDecision`: see the module
    docstring for why that enum stays two-valued.
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class InventoryCoverage(StrEnum):
    """Whether a tool inventory saw every tool the run can reach.

    An inventory built by walking a compiled graph can miss tools bound at call
    time, so a consumer must be able to tell "no dangerous tool is present" from
    "no dangerous tool was visible".
    """

    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    """What a policy is written against: a tool's name plus its content fingerprint.

    The name alone is not an identity -- it is chosen by whoever bound the tool
    and can be reused for a different callable between runs. The fingerprint is
    what makes a decision reproducible: two calls carrying the same identity
    invoked the same tool.

    Attributes:
        name: The tool's bound name, as the framework reports it.
        fingerprint: A stable digest of the tool's identifying material.
    """

    name: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolGovernanceContext:
    """The principal, tenant and run a tool call is attributed to.

    Injected rather than discovered: the enforcement stage never reads ambient
    state to work out who is calling, so a run that carries no context is a
    missing-context failure rather than an unattributed allow.

    Attributes:
        tenant_id: The tenant the run belongs to.
        principal_id: The principal the run acts as.
        run_id: The governed run's identifier.
        thread_id: The conversation thread, when the run has one. Approval
            requires a thread to resume into, so ``None`` here is what
            :class:`~zeroth.integrations.langgraph._tool_errors.ApprovalRequiresThreadError`
            reports.
        correlation_id: The gateway correlation id carried onto the run.
    """

    tenant_id: str
    principal_id: str
    run_id: str
    thread_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolAction:
    """One attempted tool call, described the way a decision is made about it.

    The pre-decision descriptor: everything the enforcement stage is allowed to
    look at, and nothing else. ``arguments`` is snapshotted on construction, so
    the mapping a decision was made against cannot change afterwards.

    Attributes:
        identity: Which tool is being invoked.
        arguments: The canonical call arguments, as an immutable snapshot.
        contract_ref: The contract the tool is bound to, when one is declared.
        principal_id: The principal the call is attributed to.
        side_effect: How the classifier rates invoking this tool.
        capability_refs: Capabilities the tool requires.
        requires_approval: Whether this tool explicitly requires approval.
        tool_call_id: The framework's stable per-call identity, when available.
    """

    identity: ToolIdentity
    arguments: Mapping[str, Any]
    contract_ref: str | None = None
    principal_id: str | None = None
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    capability_refs: Sequence[str] = ()
    requires_approval: bool = False
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        """Snapshot the arguments so a later caller-side mutation cannot reach them."""
        object.__setattr__(self, "arguments", _snapshot_mapping(self.arguments))
        object.__setattr__(self, "capability_refs", tuple(self.capability_refs))


@dataclass(frozen=True, slots=True)
class ToolDecision:
    """The verdict tool governance returned, with the reason it is recorded under.

    ``reason_code`` is the audit-side name -- bare ``snake_case``, the form
    :func:`~zeroth.governance.audit.capture_vocabulary.normalize_reason_code`
    mints -- not the ``zeroth.``-prefixed code an exception carries. Both exist
    and they are different strings.

    Attributes:
        kind: The verdict.
        reason_code: The registered reason this verdict is recorded under.
        approval_ref: The approval this decision waits on, when
            ``kind`` is :attr:`ToolDecisionKind.REQUIRE_APPROVAL`.
    """

    kind: ToolDecisionKind
    reason_code: str
    approval_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ToolInventoryEntry:
    """One tool a run can reach, as the inventory saw it.

    Attributes:
        identity: The tool's identity.
        side_effect: How the classifier rates the tool.
        contract_ref: The contract the tool is bound to, when one is declared.
        capability_refs: Capabilities the tool requires.
        requires_approval: Whether this tool explicitly requires approval.
    """

    identity: ToolIdentity
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    contract_ref: str | None = None
    capability_refs: Sequence[str] = ()
    requires_approval: bool = False

    def __post_init__(self) -> None:
        """Snapshot capability references with the rest of the entry."""
        object.__setattr__(self, "capability_refs", tuple(self.capability_refs))


@dataclass(frozen=True, slots=True)
class ToolInventory:
    """Every tool an inventory pass saw, and whether it expects it saw them all.

    ``coverage`` is part of the finding, not metadata about it: a
    :attr:`InventoryCoverage.PARTIAL` inventory cannot support "this run reaches
    no side-effecting tool", only "none was visible".

    Attributes:
        entries: The tools seen, as an immutable snapshot in discovery order.
        coverage: Whether the pass expects to have seen every reachable tool.
    """

    entries: Sequence[ToolInventoryEntry]
    coverage: InventoryCoverage = InventoryCoverage.PARTIAL

    def __post_init__(self) -> None:
        """Snapshot the entries so the inventory cannot grow after it is reported."""
        object.__setattr__(self, "entries", tuple(self.entries))


def _snapshot_mapping(source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy *source* into a mapping nobody else holds a mutable reference to.

    Built by iterating ``items()`` rather than keying back into the source, so a
    hostile mapping cannot answer one thing to the iteration and another to the
    lookup. The copy is wrapped in a read-only proxy, so the snapshot is
    immutable from both sides.

    Args:
        source: The mapping to snapshot.

    Returns:
        A read-only view over a private copy of *source*.
    """
    return MappingProxyType({key: value for key, value in source.items()})


__all__ = [
    "InventoryCoverage",
    "SideEffectClass",
    "ToolAction",
    "ToolDecision",
    "ToolDecisionKind",
    "ToolGovernanceContext",
    "ToolIdentity",
    "ToolInventory",
    "ToolInventoryEntry",
]

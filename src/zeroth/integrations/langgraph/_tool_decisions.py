"""The seam a tool-governance verdict arrives through, and what happens when it does not.

ZER-6 owns the *shape* of the seam and nothing behind it: a
:class:`ToolDecisionClient` protocol, a default implementation that denies
everything, and :func:`resolve_tool_decision`, which turns whatever a client did
-- answer, raise, return nonsense, not exist -- into exactly one
:class:`~zeroth.integrations.langgraph._tool_types.ToolDecision`. The
gateway-backed client is ZER-8's; nothing here opens a connection, imports the
gateway, or knows a transport exists.

**The seam is synchronous.** The interception points this integration already
owns are sync: :class:`~zeroth.integrations.langgraph._handler.ZerothGovernanceCallbackHandler`
is a plain ``BaseCallbackHandler`` whose ``on_tool_start`` cannot ``await``, and
:class:`~zeroth.integrations.langgraph._wrapper.GovernedGraph` wraps the sync
``invoke`` / ``stream`` entrypoints alongside the async ones. A sync consumer can
never call an async seam, while an async consumer can always call a sync one --
so the seam is sync, and a network-backed client bridges on its own side.

**Everything that is not an explicit allow is a denial.** There is no branch
anywhere below that reads "not configured, therefore proceed". The default
client denies; a missing client denies; a client that raises denies; a client
that returns ``None``, a duck-typed impostor or an unrecognized verdict denies;
an unclassified tool denies. The single escape is
:attr:`UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS`, which is an enum member
compared by identity -- not a boolean, not a truthy default, and not something a
stray ``True`` or a bare string can stand in for.

Every reason code minted here is a member of
:data:`~zeroth.governance.audit.capture_vocabulary.REASON_CODES`. An unregistered
code is replaced by a digest at the audit capture boundary, which is how a
denial comes to look recorded while its reason is gone -- the ZER-5 bug. A
client-supplied code that is not registered is therefore replaced, but the
*verdict* it came with is never changed by that repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from zeroth.governance.audit.capture_vocabulary import REASON_CODES
from zeroth.integrations.langgraph._tool_normalize import (
    normalize_identifier,
    require_governance_context,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)

_POLICY_UNAVAILABLE = "policy_unavailable"
"""Recorded when no verdict could be obtained: no client, a raise, or nonsense back."""

_POLICY_VIOLATION = "policy_violation"
"""Recorded when this module's own gate denied the call."""

_UNSPECIFIED_REASON = "unknown_error"
"""Stands in for a client-supplied reason code that is not registered.

The verdict survives the substitution untouched. Repairing a code must never
turn a denial into an allow, nor an allow into a denial: it only decides which
registered term the decision is *recorded* under.
"""


class UnknownSideEffectPolicy(StrEnum):
    """Whether a tool nobody classified may be invoked at all.

    The named opt-in, and the only one. It is an enum member rather than a
    boolean so that no default, no truthy value and no misplaced argument can
    switch it on by accident, and it is compared by identity rather than
    equality because ``StrEnum`` members equal their string values -- ``==``
    would admit the bare literal ``"allow_unclassified_tools"`` from anywhere.
    Enums with members cannot be subclassed, so the set of answers is closed.
    """

    DENY = "deny"
    ALLOW_UNCLASSIFIED_TOOLS = "allow_unclassified_tools"


class ToolDecisionClient(Protocol):
    """Whatever can return a verdict for one normalized tool action.

    Implementations receive an action that is already fully normalized: the
    identity is pinned, the arguments are canonical, the principal is injected.
    They are never handed raw call material and are never asked to normalize.
    """

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the verdict for *action* as attributed by *context*."""
        ...


@dataclass(frozen=True, slots=True)
class FailClosedToolDecisionClient:
    """The client in force until a real one is injected: it denies every call.

    Not a stub to be replaced by an allow-by-default while the real client is
    being built -- it is the correct behaviour for a governed runtime whose
    policy source is absent. A deployment that has not wired up a decision
    client has not decided that its tools are safe.
    """

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Deny *action*, because no policy source is available to allow it."""
        return ToolDecision(kind=ToolDecisionKind.DENY, reason_code=_POLICY_UNAVAILABLE)


def _denied(reason_code: str) -> ToolDecision:
    """Build a denial recorded under *reason_code*."""
    return ToolDecision(kind=ToolDecisionKind.DENY, reason_code=reason_code)


def _registered_reason(reason_code: object) -> str:
    """Return *reason_code* if it is a registered audit term, else the fallback."""
    if type(reason_code) is str and reason_code in REASON_CODES:
        return reason_code
    return _UNSPECIFIED_REASON


def _accepted(decision: object) -> ToolDecision:
    """Return a client's verdict rebuilt from gated fields, or a denial.

    A verdict is only recognized as one when it is exactly a ``ToolDecision``
    carrying exactly a ``ToolDecisionKind``. That single gate covers ``None``, a
    duck-typed impostor, a subclass overriding attribute access, and a verdict
    from some other enum -- each of which resolves to a denial rather than to
    whatever the object claimed.
    """
    if type(decision) is not ToolDecision or type(decision.kind) is not ToolDecisionKind:
        return _denied(_POLICY_UNAVAILABLE)
    return ToolDecision(
        kind=decision.kind,
        reason_code=_registered_reason(decision.reason_code),
        approval_ref=normalize_identifier(decision.approval_ref),
    )


def _client_decision(
    client: ToolDecisionClient | None,
    action: ToolAction,
    context: ToolGovernanceContext,
) -> ToolDecision:
    """Ask *client* for a verdict, converting every way that can fail into a denial.

    A caller that injected no client gets :class:`FailClosedToolDecisionClient`,
    so the absent-client path runs the same code as every other client rather
    than a special case that could be relaxed independently.
    """
    resolved = FailClosedToolDecisionClient() if client is None else client
    try:
        decision = resolved.decide(action, context)
    except Exception:
        # Any failure reaching the policy source is a denial, never a pass:
        # a client that cannot answer has not answered "allow". BaseException
        # (cancellation, KeyboardInterrupt) is deliberately not caught -- that
        # is the run being torn down, not a verdict.
        return _denied(_POLICY_UNAVAILABLE)
    return _accepted(decision)


def _classified(value: object) -> bool:
    """Return whether *value* is a positive side-effect classification.

    Exact-typed, because the ``ToolAction`` this reads is only *usually* the one
    :func:`~zeroth.integrations.langgraph._tool_normalize.normalize_tool_action`
    built. A caller that constructs the dataclass directly -- and this module's
    own gate admits an exactly-typed ``ToolAction`` from anywhere -- can put the
    bare string ``"unknown"``, ``None``, or any other value in the field, and
    ``StrEnum`` members compare equal to their own strings. A ``!= UNKNOWN`` test
    therefore reads every one of those as *classified* and lets it through to the
    client, which is an unclassified tool reaching an allow. Anything that is not
    an actual member of the enum is unknown here, and unknown is what the default
    policy refuses.

    Args:
        value: The action's side-effect field.

    Returns:
        Whether somebody positively classified this tool.
    """
    return type(value) is SideEffectClass and value is not SideEffectClass.UNKNOWN


def _denies_unclassified(action: ToolAction, policy: object) -> bool:
    """Return whether an unclassified tool must be refused under *policy*."""
    if _classified(action.side_effect):
        return False
    return policy is not UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS


def resolve_tool_decision(
    action: object,
    context: object,
    client: ToolDecisionClient | None = None,
    *,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
) -> ToolDecision:
    """Resolve exactly one verdict for an already-normalized tool action.

    Pure: it decides and returns. It writes no audit record, raises nothing to
    halt execution, requests no approval and touches no inventory -- those are
    the enforcement stage's, which consumes this verdict.

    Args:
        action: The normalized action, as built by
            :func:`~zeroth.integrations.langgraph._tool_normalize.normalize_tool_action`.
        context: The injected governance context the call is attributed to.
        client: The decision client, or ``None`` to deny for want of one.
        unknown_side_effect: Whether a tool nobody classified may be invoked.

    Returns:
        The verdict, which is a denial unless a recognized client explicitly
        allowed the call.

    Raises:
        GovernanceContextError: If the call cannot be attributed to a principal.
            An unattributable call is not denied, it is undecidable: there is
            nobody to record the denial against.
    """
    governance = require_governance_context(context)
    if type(action) is not ToolAction:
        return _denied(_POLICY_UNAVAILABLE)
    if _denies_unclassified(action, unknown_side_effect):
        return _denied(_POLICY_VIOLATION)
    return _client_decision(client, action, governance)


__all__ = [
    "FailClosedToolDecisionClient",
    "ToolDecisionClient",
    "UnknownSideEffectPolicy",
    "resolve_tool_decision",
]

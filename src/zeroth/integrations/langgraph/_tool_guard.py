"""The one place a governed LangGraph tool call is allowed, refused or paused.

Both tool-governance surfaces -- the ``govern_tools`` wrapper and
``ZerothMiddleware`` -- call :func:`guard_tool_call`, and neither carries an
enforcement branch of its own. Two implementations of "may this tool run" is two
places for the fail-closed rules to diverge, and the one that drifts is the one
that fails *open*: a surface that forgot the no-thread rule turns an approval
into an allow and nobody sees a difference until an unapprovable call has already
run.

**The core is in the call path, not beside it.** :func:`guard_tool_call` receives
the downstream invocation as a callable and invokes it exactly once on an allow.
It wraps that call only to persist fenced success or failure, never in a loop: an
exception the tool raises propagates unchanged, and a tool that failed is never
retried. Governance decides whether a call happens; it does not decide what the
call means. :func:`aguard_tool_call` owns the same boundary for awaited work.

**The pause seam is injected, exactly like the decision seam.** LangGraph's
``interrupt`` arrives as a parameter defaulting to :func:`_langgraph_interrupt`,
which imports it on use. An import performed inside the enforcement function
would leave no module attribute for a test to substitute, so the approval branch
would be untestable without the dependency installed -- and a test that appears
to patch nothing asserts nothing. Injection keeps every test here free of
``langgraph``.

**Approval completion follows execution.** The initial pass persists the request
before interrupting. A resumed pass consumes only the current fenced resolution,
validates any edit, rechecks policy, and marks the lifecycle resolved only after
the downstream tool returns successfully. A tool failure or stale delivery can
therefore never become a replayable success.

**Audit travels the typed ``tool_calls`` field, never a new metadata key.**
``execution_metadata`` is an allowlist that drops unrecognized keys silently
(:data:`~zeroth.governance.audit.capture_vocabulary.METADATA_KINDS`), and it has
no key for a tool name, a tool reference or an approval id. Writing one would
produce a record that looks audited and carries no evidence -- the ZER-5 failure
shape. :class:`~zeroth.governance.audit.models.ToolCallRecord` and
:class:`~zeroth.governance.audit.models.ApprovalActionRecord` already carry
exactly those fields, are ``extra="forbid"``, and travel the capture path's typed
branch. So the projection puts the tool in ``tool_calls``, the approval in
``approval_actions``, and only ``decision`` / ``reason_code`` -- both long-standing
allowlisted keys -- in the metadata.

**The one vocabulary term this adds is ``require_approval``.** ``decision``
already retains ``allow`` and ``deny``, so neither of those needs anything: an
allow records as ``decision: "allow"`` and never as a ``reason_code``, which is
for failures and denial branches. The vocabulary's ``approve`` / ``reject`` are
:class:`~zeroth.contracts.governed.models.approval.ApprovalDecisionType`
*resolutions* -- what a human answered -- and cannot stand in for "this call is
waiting on a human", so ``require_approval`` is added to
``METADATA_VOCABULARIES["decision"]``. The addition is safe by construction: the
vocabulary tests are subset-shaped (each asserts its source enum's members are
*covered*, never that the set is exactly them) and the label-shape test only
requires a lower-case, stripped, non-empty term.

Every value written into metadata or into the interrupt payload is a plain
``str``, ``int`` or ``None``, never a ``StrEnum`` member. The capture projection
gates a vocabulary value with ``type(value) is str``
(``capture_projection.py``), so an enum member -- which compares and hashes equal
to its own string -- passes a membership test and is then summarized away at
write time. ``.value`` is the difference between a retained decision and a
digest.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from zeroth.governance.audit.models import (
    ApprovalActionRecord,
    NodeAuditRecord,
    ToolCallRecord,
)
from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalState,
    SQLiteApprovalRepository,
    _current_resume_claim,
)
from zeroth.integrations.langgraph._tool_decisions import (
    ToolDecisionClient,
    UnknownSideEffectPolicy,
    resolve_tool_decision,
)
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_normalize import (
    argument_fingerprint,
    canonical_arguments,
    normalize_identifier,
    require_governance_context,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolIdentity,
)

logger = logging.getLogger(__name__)

TOOL_GUARD_NODE_ID = "langgraph.tool_guard"
"""The node identity every decision record this module writes is attributed to.

The record describes the *decision* stage, not the tool's execution: it is
projected before the downstream call and never carries an outcome, because the
core does not observe one. Wrapping the invocation to collect a result is exactly
the swallow-or-retry surface R4 forbids.
"""

_GRAPH_VERSION_REF = "langgraph.external"
"""Where the graph came from: outside the platform's own version registry.

The gateway's audit sink already writes this literal for the same reason -- a
caller-owned LangGraph app has no Zeroth graph version -- and reusing it keeps the
two externally-originated record families queryable as one.
"""

_DEPLOYMENT_REF = "langgraph.external"
"""The deployment a caller-owned graph belongs to, which the platform never made."""

_APPROVAL_PAYLOAD_VERSION = 1
"""The interrupt payload's schema version, carried so a resumer can refuse an old one."""

_APPROVAL_PAYLOAD_KIND = "tool_approval"
"""What the interrupt payload is about, so an unrelated interrupt is distinguishable."""

_APPROVAL_REQUESTED = "requested"
"""The ``ApprovalActionRecord.action`` term for an approval this stage asked for."""

_APPROVAL_APPROVE = "approve"
_APPROVAL_REJECT = "reject"

_STATUS_ALLOWED = "completed"
_STATUS_DENIED = "rejected"
_STATUS_WAITING = "waiting_approval"


class ToolAuditSubmitter(Protocol):
    """The non-blocking hand-off a decided tool call is projected into.

    Structurally satisfied by
    :class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue`. An
    implementation must not block or perform I/O: enforcement runs inline in a
    graph node, so a submitter that waits on a durable write puts the audit
    system's latency onto every governed tool call.
    """

    def submit(self, record: NodeAuditRecord) -> Any:
        """Queue one record for durable audit and return without awaiting it."""
        ...


def _langgraph_interrupt(payload: Mapping[str, Any]) -> Any:
    """Suspend the run through LangGraph's own ``interrupt``, imported on use.

    The import is deferred so that importing this module -- and testing every
    branch in it -- does not require ``langgraph`` to be installed.

    Args:
        payload: The approval request handed to whoever resumes the run.

    Returns:
        Whatever LangGraph returns on resume. On the first pass it does not
        return at all: it raises ``GraphInterrupt``.
    """
    from langgraph.types import interrupt

    return interrupt(payload)


def _require_stable_identity(action: object) -> ToolAction:
    """Return *action* only when it is pinned to an identity a policy can name.

    Structural, deliberately: a ``ToolAction`` carries no identifying material,
    so nothing here can recompute a fingerprint to check it. What it can check is
    that an identity is present, exactly typed and non-blank -- an action whose
    name or fingerprint is missing cannot carry a reproducible decision, and a
    decision that cannot be reproduced is not one an auditor can rely on.

    Args:
        action: The candidate normalized action.

    Returns:
        The action, unchanged.

    Raises:
        UnstableToolIdentityError: If the action or its identity is not exactly
            the expected type, or either identity field is unusable.
    """
    if type(action) is not ToolAction:
        raise UnstableToolIdentityError("a governed tool call needs a normalized ToolAction")
    identity = action.identity
    if type(identity) is not ToolIdentity:
        raise UnstableToolIdentityError("tool identity is not a ToolIdentity")
    if normalize_identifier(identity.name) is None:
        raise UnstableToolIdentityError("tool name is not a usable identifier")
    if normalize_identifier(identity.fingerprint) is None:
        raise UnstableToolIdentityError("tool fingerprint is not a usable identifier")
    if (
        action.tool_call_id is not None
        and normalize_identifier(action.tool_call_id) != action.tool_call_id
    ):
        raise UnstableToolIdentityError("tool-call id is not a stable identifier")
    return action


def _require_matching_principal(action: ToolAction, governance: ToolGovernanceContext) -> None:
    """Refuse a call normalized against one principal and decided against another.

    Normalization and enforcement each take a context, and nothing outside this
    check makes them the same context. A caller that normalized as principal A
    and enforced as principal B would produce a decision recorded against B about
    a call attributed to A -- attribution confusion, not privilege escalation, but
    the kind an auditor cannot unpick after the fact.

    The mismatch is a :class:`GovernanceContextError` rather than a denial for the
    reason ``resolve_tool_decision`` already gives: an unattributable call is not
    denied, it is undecidable, because there is nobody to record the denial
    against.

    Args:
        action: The normalized action.
        governance: The normalized governance context enforcement is running under.

    Raises:
        GovernanceContextError: If the action carries no plain-``str`` principal,
            or one that differs from the context's.
    """
    principal = action.principal_id
    if type(principal) is not str or principal != governance.principal_id:
        raise GovernanceContextError("the tool action's principal does not match its context")


def _recognized_decision(decision: object) -> ToolDecision:
    """Return a verdict only when it is exactly one, refusing the call otherwise.

    ``resolve_tool_decision`` already rebuilds every verdict from gated fields, so
    on the composed path nothing unrecognized can arrive. This gate is what makes
    that a property of the *enforcement* rather than of one caller: a surface that
    obtained a decision some other way still cannot enforce a duck-typed
    impostor, a ``ToolDecision`` subclass overriding attribute access, or a
    verdict from a foreign enum whose ``allow`` spells the same.

    Args:
        decision: The candidate verdict.

    Returns:
        The verdict, unchanged.

    Raises:
        PolicyViolation: If *decision* is not exactly a ``ToolDecision`` carrying
            exactly a ``ToolDecisionKind``. The call is refused rather than
            reported as broken, because the outcome the caller must observe is
            the same one a denial produces.
    """
    if type(decision) is not ToolDecision or type(decision.kind) is not ToolDecisionKind:
        raise PolicyViolation("tool governance returned no recognizable verdict")
    return decision


def _reason_term(decision: ToolDecision) -> str:
    """Return a decision's reason as a plain, bounded ``str``."""
    return normalize_identifier(decision.reason_code) or "unknown_error"


def _side_effect_term(value: object) -> str:
    """Return a side-effect classification as a plain ``str``, defaulting to unknown."""
    if type(value) is not SideEffectClass:
        return SideEffectClass.UNKNOWN.value
    return value.value


def _decision_status(kind: ToolDecisionKind) -> str:
    """Return the record status one verdict is stored under.

    Compared by identity and defaulting to the denied status, so a member this
    function was never taught about records as a refusal rather than as a
    completion.
    """
    if kind is ToolDecisionKind.ALLOW:
        return _STATUS_ALLOWED
    if kind is ToolDecisionKind.REQUIRE_APPROVAL:
        return _STATUS_WAITING
    return _STATUS_DENIED


def _approval_reference(decision: ToolDecision, governance: ToolGovernanceContext) -> str | None:
    """Return the approval a verdict waits on, refusing one that cannot be answered.

    Both refusals here run *before* anything is recorded or interrupted. A record
    saying "waiting for approval" about a call that could never wait would be
    worse than no record; the exception itself is audited by the runtime under a
    registered reason code (``approval_requires_thread_error``,
    ``tool_governance_error``).

    Args:
        decision: The verdict.
        governance: The normalized governance context.

    Returns:
        The normalized approval reference, or ``None`` when the verdict is not an
        approval request.

    Raises:
        ApprovalRequiresThreadError: If the run has no thread to resume into. An
            approval that cannot be resumed must never quietly become an allow.
        ToolGovernanceError: If the verdict carries no usable approval reference,
            leaving nothing for a resolution to be recorded against.
    """
    if decision.kind is not ToolDecisionKind.REQUIRE_APPROVAL:
        return None
    if governance.thread_id is None:
        raise ApprovalRequiresThreadError("a tool call awaiting approval needs a thread")
    reference = normalize_identifier(decision.approval_ref)
    if reference is None:
        raise ToolGovernanceError("an approval verdict must carry a resumable approval reference")
    return reference


def _approval_payload(
    action: ToolAction,
    governance: ToolGovernanceContext,
    decision: ToolDecision,
    approval_ref: str,
) -> dict[str, Any]:
    """Build the versioned, JSON-serializable request handed to ``interrupt``.

    Every value is a plain ``str``, ``int`` or ``None``, so the payload survives
    a checkpoint's serialization round trip byte-identically. The call's
    arguments are represented by their fingerprint rather than by their content:
    the payload is written into graph state that outlives the run, and a digest
    still proves the resumed call is the one that was approved.

    Args:
        action: The normalized action awaiting approval.
        governance: The normalized governance context.
        decision: The verdict that asked for approval.
        approval_ref: The approval this request is recorded against.

    Returns:
        The approval request payload.
    """
    return {
        "version": _APPROVAL_PAYLOAD_VERSION,
        "kind": _APPROVAL_PAYLOAD_KIND,
        "approval_ref": approval_ref,
        **_approval_action_identity(action, governance),
        "correlation_id": governance.correlation_id,
        "tool_name": action.identity.name,
        "contract_ref": normalize_identifier(action.contract_ref),
        "side_effect": _side_effect_term(action.side_effect),
        "reason_code": _reason_term(decision),
    }


def _approval_action_identity(
    action: ToolAction, governance: ToolGovernanceContext
) -> dict[str, Any]:
    """Return the persisted fields that identify one replayed tool action."""
    return {
        "tenant_id": governance.tenant_id,
        "principal_id": governance.principal_id,
        "run_id": governance.run_id,
        "thread_id": governance.thread_id,
        "tool_fingerprint": action.identity.fingerprint,
        "tool_call_id": action.tool_call_id,
        "argument_fingerprint": argument_fingerprint(action.arguments),
    }


def _decision_metadata(decision: ToolDecision, decision_term: str | None = None) -> dict[str, str]:
    """Render one verdict as allowlisted, content-free metadata.

    ``reason_code`` is written only for a denial. It names a failure or a denial
    branch, so putting it on an allow would record ``unknown_error`` -- the
    fallback for an unregistered term -- against a call nothing went wrong with.
    An approval request's reason belongs to the approval the ``approval_ref``
    points at, not to a failure vocabulary.
    """
    metadata = {"decision": decision.kind.value if decision_term is None else decision_term}
    if decision.kind is ToolDecisionKind.DENY:
        metadata["reason_code"] = _reason_term(decision)
    return metadata


def _tool_call_record(action: ToolAction, decision: ToolDecision) -> ToolCallRecord:
    """Project the decided call onto the typed field that survives capture.

    ``outcome`` is always absent: the record is written before the tool runs and
    the core never observes what it returned.
    """
    return ToolCallRecord(
        tool_ref=action.identity.fingerprint,
        alias=action.identity.name,
        arguments=dict(action.arguments),
        outcome=None,
        error=_reason_term(decision) if decision.kind is ToolDecisionKind.DENY else None,
    )


def _approval_records(
    approval_ref: str | None,
    actor: ActorIdentity | None,
    action: str = _APPROVAL_REQUESTED,
) -> list[ApprovalActionRecord]:
    """Project the requested approval onto the typed field that carries its id."""
    if approval_ref is None:
        return []
    return [ApprovalActionRecord(approval_id=approval_ref, action=action, actor=actor)]


def _project(
    action: ToolAction,
    governance: ToolGovernanceContext,
    decision: ToolDecision,
    *,
    audit_id: str,
    actor: ActorIdentity | None,
    approval_ref: str | None,
    decision_term: str | None = None,
    approval_action: str = _APPROVAL_REQUESTED,
) -> NodeAuditRecord:
    """Build the record for one decided tool call.

    Takes the identity as an argument instead of minting one, following the
    gateway's audit sink: a projection that minted its own could be called twice
    for one logical decision and persist it twice.

    The actor is injected rather than reconstructed from the principal. Building
    one here would mean choosing an
    :class:`~zeroth.governance.identity.AuthMethod`, and this stage did not
    authenticate anybody -- a guessed auth method is a claim about evidence that
    does not exist.
    """
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=governance.run_id,
        thread_id=governance.thread_id,
        node_id=TOOL_GUARD_NODE_ID,
        graph_version_ref=_GRAPH_VERSION_REF,
        deployment_ref=_DEPLOYMENT_REF,
        # Explicit, never the model's "default" tenant: that one owns the
        # fallback retention policy, so inheriting it silently reassigns a
        # tenant's evidence.
        tenant_id=governance.tenant_id,
        status=_decision_status(decision.kind),
        actor=actor,
        execution_metadata=_decision_metadata(decision, decision_term),
        tool_calls=[_tool_call_record(action, decision)],
        approval_actions=_approval_records(approval_ref, actor, approval_action),
    )


def _emit_decision_audit(
    audit: ToolAuditSubmitter | None,
    action: ToolAction,
    governance: ToolGovernanceContext,
    decision: ToolDecision,
    *,
    actor: ActorIdentity | None,
    approval_ref: str | None,
    decision_term: str | None = None,
    approval_action: str = _APPROVAL_REQUESTED,
) -> None:
    """Hand one decided tool call off for durable audit, without ever raising.

    Emission happens *before* enforcement acts on the verdict, so the denial
    record exists even though the denial raises, and the approval record is
    submitted before the interrupt propagates out of the node.

    Nothing here can fail the call it is recording. A projection that will not
    validate and a submitter that refuses or raises are both counted as one lost
    record, logged under a fixed code and the exception's type -- never its
    message, which is foreign text -- because a governed call must not start
    failing because the audit stage did.
    """
    if audit is None:
        return
    audit_id = f"{TOOL_GUARD_NODE_ID}:{uuid4().hex}"
    try:
        record = _project(
            action,
            governance,
            decision,
            audit_id=audit_id,
            actor=actor,
            approval_ref=approval_ref,
            decision_term=decision_term,
            approval_action=approval_action,
        )
    except Exception as error:
        logger.error(
            "tool guard audit projection failed audit_id=%s exception_type=%s",
            audit_id,
            type(error).__name__,
        )
        return
    try:
        audit.submit(record)
    except Exception as error:
        logger.error(
            "tool guard audit submit failed audit_id=%s exception_type=%s",
            audit_id,
            type(error).__name__,
        )


@dataclass(frozen=True, slots=True)
class _Authorization:
    decision: ToolDecision
    action: ToolAction
    governance: ToolGovernanceContext
    approval_ref: str | None = None
    claim_token: str | None = None
    edited: bool = False


def _suspend_for_approval(
    action: ToolAction,
    governance: ToolGovernanceContext,
    decision: ToolDecision,
    approval_ref: str,
    interrupt: Callable[[Mapping[str, Any]], Any] | None,
    lifecycle: SQLiteApprovalRepository,
    client: ToolDecisionClient | None,
    unknown_side_effect: UnknownSideEffectPolicy,
    audit: ToolAuditSubmitter | None,
    actor: ActorIdentity | None,
    prepare_edited_arguments: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    replay: ApprovalRecord | None = None,
    resume_claim: tuple[str, str] | None = None,
) -> tuple[ToolAction, str, str]:
    """Suspend, consume the current fence, and revalidate the exact resumed call."""
    if replay is None:
        payload = _approval_payload(action, governance, decision, approval_ref)
        record, created = lifecycle.begin_once(payload, action.arguments)
        approval_ref = normalize_identifier(record.intent.payload.get("approval_ref"))
        if approval_ref is None:
            raise ToolGovernanceError("persisted approval has no resumable reference")
        payload = dict(record.intent.payload)
    else:
        replay_ref = normalize_identifier(replay.intent.payload.get("approval_ref"))
        resume_ref = None if resume_claim is None else resume_claim[0]
        if replay_ref != resume_ref and argument_fingerprint(
            replay.intent.arguments
        ) != argument_fingerprint(action.arguments):
            raise ToolGovernanceError("approval replay arguments changed before delivery")
        payload, created = dict(replay.intent.payload), False
    if created:
        _emit_decision_audit(
            audit,
            action,
            governance,
            decision,
            actor=actor,
            approval_ref=approval_ref,
        )
    suspend = _langgraph_interrupt if interrupt is None else interrupt
    resumed = suspend(payload)
    delivery = lifecycle.consume(resumed)
    resolution, claim_token = delivery.resolution, delivery.claim_token
    expected_delivery = (approval_ref, claim_token) if resume_claim is None else resume_claim
    if (resolution.approval_ref, claim_token) != expected_delivery:
        lifecycle.fail(resolution.approval_ref, claim_token)
        raise ToolGovernanceError("approval delivery does not match the resumed action")
    approval_ref = expected_delivery[0]
    if resolution.decision is ApprovalDecision.REJECT:
        lifecycle.finish(approval_ref, claim_token)
        _emit_decision_audit(
            audit,
            action,
            governance,
            ToolDecision(ToolDecisionKind.DENY, "policy_violation"),
            actor=actor,
            approval_ref=approval_ref,
            decision_term=_APPROVAL_REJECT,
            approval_action=_APPROVAL_REJECT,
        )
        raise PolicyViolation("approval rejected this tool call")
    if resume_claim is not None:
        lifecycle._replay_for_claim(*resume_claim, _approval_action_identity(action, governance))
    try:
        arguments = action.arguments
        if resolution.arguments is not None:
            arguments = (
                canonical_arguments(resolution.arguments)
                if prepare_edited_arguments is None
                else canonical_arguments(prepare_edited_arguments(resolution.arguments))
            )
        approved = replace(action, arguments=arguments)
        fresh = _recognized_decision(
            resolve_tool_decision(
                approved,
                governance,
                client,
                unknown_side_effect=unknown_side_effect,
            )
        )
    except BaseException:
        lifecycle.fail(approval_ref, claim_token)
        raise
    if fresh.kind is ToolDecisionKind.DENY or (
        fresh.kind is ToolDecisionKind.REQUIRE_APPROVAL
        and normalize_identifier(fresh.approval_ref) != approval_ref
    ):
        lifecycle.finish(approval_ref, claim_token)
        _emit_decision_audit(
            audit,
            approved,
            governance,
            fresh,
            actor=actor,
            approval_ref=approval_ref,
            approval_action=_APPROVAL_APPROVE,
        )
        raise PolicyViolation("fresh policy refused the approved tool call")
    return approved, claim_token, approval_ref


def _enforce(
    action: ToolAction,
    governance: ToolGovernanceContext,
    decision: ToolDecision,
    approval_ref: str | None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None,
) -> None:
    """Act on one verdict, returning only when the call may proceed.

    Every path that is not an explicit allow ends in a raise, including a verdict
    kind this function was never taught about: the denial is the fall-through, so
    a member added to :class:`ToolDecisionKind` without updating this function
    refuses calls rather than admitting them.
    """
    kind = decision.kind
    if kind is ToolDecisionKind.ALLOW:
        return
    raise PolicyViolation("policy refused this tool call")


def _authorize_tool_action(
    action: object,
    context: object,
    *,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    approval_lifecycle: SQLiteApprovalRepository | None = None,
    prepare_edited_arguments: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> _Authorization:
    """Return the verdict and exact action authorized for execution."""
    governance = require_governance_context(context)
    normalized = _require_stable_identity(action)
    _require_matching_principal(normalized, governance)
    action_identity = _approval_action_identity(normalized, governance)
    resume_claim = _current_resume_claim()
    replay = (
        approval_lifecycle.replay_for(action_identity)
        if type(approval_lifecycle) is SQLiteApprovalRepository
        else None
    )
    if replay is not None and replay.state in (ApprovalState.RESOLVED, ApprovalState.ORPHANED):
        raise ToolGovernanceError("approval terminal delivery permanently fences this action")
    if resume_claim is not None:
        if type(approval_lifecycle) is not SQLiteApprovalRepository:
            raise ApprovalRequiresThreadError("approval needs its durable lifecycle store")
        claimed = approval_lifecycle._claimed_replay(*resume_claim)
        if claimed is None:
            if replay is not None and replay.intent.payload.get("approval_ref") == resume_claim[0]:
                replay = None
            resume_claim = None
        elif replay is None:
            replay = claimed
    if replay is not None:
        approval_ref = normalize_identifier(replay.intent.payload.get("approval_ref"))
        if approval_ref is None:
            raise ToolGovernanceError("persisted approval has no resumable reference")
        decision = ToolDecision(
            ToolDecisionKind.REQUIRE_APPROVAL,
            normalize_identifier(replay.intent.payload.get("reason_code")) or "policy_violation",
            approval_ref,
        )
        approved, claim_token, approval_ref = _suspend_for_approval(
            normalized,
            governance,
            decision,
            approval_ref,
            interrupt,
            approval_lifecycle,
            client,
            unknown_side_effect,
            audit,
            actor,
            prepare_edited_arguments,
            replay,
            resume_claim,
        )
        return _Authorization(
            decision,
            approved,
            governance,
            approval_ref,
            claim_token,
            argument_fingerprint(approved.arguments) != argument_fingerprint(normalized.arguments),
        )
    decision = _recognized_decision(
        resolve_tool_decision(
            normalized, governance, client, unknown_side_effect=unknown_side_effect
        )
    )
    approval_ref = _approval_reference(decision, governance)
    if (
        decision.kind is ToolDecisionKind.REQUIRE_APPROVAL
        and type(approval_lifecycle) is not SQLiteApprovalRepository
    ):
        raise ApprovalRequiresThreadError("approval needs a durable lifecycle store")
    if decision.kind is ToolDecisionKind.REQUIRE_APPROVAL and approval_ref is not None:
        assert approval_lifecycle is not None
        approved, claim_token, approval_ref = _suspend_for_approval(
            normalized,
            governance,
            decision,
            approval_ref,
            interrupt,
            approval_lifecycle,
            client,
            unknown_side_effect,
            audit,
            actor,
            prepare_edited_arguments,
        )
        return _Authorization(
            decision,
            approved,
            governance,
            approval_ref,
            claim_token,
            argument_fingerprint(approved.arguments) != argument_fingerprint(normalized.arguments),
        )
    _emit_decision_audit(
        audit, normalized, governance, decision, actor=actor, approval_ref=approval_ref
    )
    _enforce(normalized, governance, decision, approval_ref, interrupt)
    return _Authorization(decision, normalized, governance)


def _fail_authorization(
    authorization: _Authorization, lifecycle: SQLiteApprovalRepository | None
) -> None:
    if lifecycle is None or authorization.approval_ref is None or authorization.claim_token is None:
        return
    with suppress(Exception):
        lifecycle.fail(authorization.approval_ref, authorization.claim_token)


def _complete_authorization(
    authorization: _Authorization,
    lifecycle: SQLiteApprovalRepository | None,
    audit: ToolAuditSubmitter | None,
    actor: ActorIdentity | None,
) -> None:
    if lifecycle is None or authorization.approval_ref is None or authorization.claim_token is None:
        return
    lifecycle.finish(authorization.approval_ref, authorization.claim_token)
    _emit_decision_audit(
        audit,
        authorization.action,
        authorization.governance,
        ToolDecision(ToolDecisionKind.ALLOW, "unknown_error"),
        actor=actor,
        approval_ref=authorization.approval_ref,
        decision_term=_APPROVAL_APPROVE,
        approval_action=_APPROVAL_APPROVE,
    )


def authorize_tool_call(
    action: object,
    context: object,
    *,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    approval_lifecycle: SQLiteApprovalRepository | None = None,
) -> ToolDecision:
    """Decide and enforce one tool call when no invocation boundary is needed.

    Approval resume is refused here because completion cannot be proven without
    owning the downstream call. Sync and async invoking surfaces use
    :func:`guard_tool_call` and :func:`aguard_tool_call` instead.

    Args:
        action: The normalized action, as built by
            :func:`~zeroth.integrations.langgraph._tool_normalize.normalize_tool_action`.
        context: The injected governance context the call is attributed to.
        client: The decision client, or ``None`` to deny for want of one.
        unknown_side_effect: Whether a tool nobody classified may be invoked.
        audit: Where the decision record is handed off, or ``None`` to decide
            without recording.
        actor: The authenticated actor to attribute the record to, when the
            calling surface has one.
        interrupt: The pause seam, defaulting to LangGraph's ``interrupt``.
        approval_lifecycle: Durable approval storage used before an interrupt.

    Returns:
        The allow verdict. Every other verdict raises.

    Raises:
        GovernanceContextError: If the call cannot be attributed, or was
            normalized against a different principal than it is enforced under.
        UnstableToolIdentityError: If the action carries no usable tool identity.
        PolicyViolation: If policy denied the call, or returned no recognizable
            verdict.
        ApprovalRequiresThreadError: If the call needs approval and the run has
            no thread to resume into.
        ToolGovernanceError: If an approval verdict carries no usable reference,
            or the pause seam returned instead of suspending.
    """
    authorization = _authorize_tool_action(
        action,
        context,
        client=client,
        unknown_side_effect=unknown_side_effect,
        audit=audit,
        actor=actor,
        interrupt=interrupt,
        approval_lifecycle=approval_lifecycle,
    )
    if authorization.claim_token is not None:
        _fail_authorization(authorization, approval_lifecycle)
        raise ToolGovernanceError("approved execution requires a guarded invocation")
    return authorization.decision


def authorize_tool_action(action: object, context: object, **seams: Any) -> ToolAction:
    """Authorize and return the immutable original or approved edited action."""
    authorization = _authorize_tool_action(action, context, **seams)
    if authorization.claim_token is not None:
        _fail_authorization(authorization, seams.get("approval_lifecycle"))
        raise ToolGovernanceError("approved execution requires a guarded invocation")
    return authorization.action


def guard_tool_call(
    action: object,
    context: object,
    invoke: Callable[[], Any],
    *,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    approval_lifecycle: SQLiteApprovalRepository | None = None,
    invoke_with_arguments: Callable[[Mapping[str, Any]], Any] | None = None,
    prepare_edited_arguments: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> Any:
    """Govern one tool call and, if it is allowed, make it exactly once.

    **The entry point.** Every tool-governance surface calls this rather than
    composing the pieces itself.

    ``invoke`` is called once and never in a retry loop. An exception it raises
    propagates unchanged after the current delivery is fenced as failed. On any
    other verdict it is not called at all, because the raise happens before it.

    Args:
        action: The normalized action, as built by
            :func:`~zeroth.integrations.langgraph._tool_normalize.normalize_tool_action`.
        context: The injected governance context the call is attributed to.
        invoke: The downstream tool invocation, called only on an allow.
        client: The decision client, or ``None`` to deny for want of one.
        unknown_side_effect: Whether a tool nobody classified may be invoked.
        audit: Where the decision record is handed off, or ``None`` to decide
            without recording.
        actor: The authenticated actor to attribute the record to, when the
            calling surface has one.
        interrupt: The pause seam, defaulting to LangGraph's ``interrupt``.
        approval_lifecycle: Durable approval storage used before an interrupt.
        invoke_with_arguments: Invocation seam for approved edited arguments.
        prepare_edited_arguments: Native validation seam for edited arguments.

    Returns:
        Whatever the downstream invocation returned.

    Raises:
        ToolGovernanceError: Whenever the call did not proceed. See
            :func:`authorize_tool_call` for which subclass names which condition.
    """
    authorization = _authorize_tool_action(
        action,
        context,
        client=client,
        unknown_side_effect=unknown_side_effect,
        audit=audit,
        actor=actor,
        interrupt=interrupt,
        approval_lifecycle=approval_lifecycle,
        prepare_edited_arguments=prepare_edited_arguments,
    )
    try:
        if authorization.edited:
            if invoke_with_arguments is None:
                raise ToolGovernanceError(
                    "edited tool arguments cannot be reissued on this surface"
                )
            result = invoke_with_arguments(authorization.action.arguments)
        else:
            result = invoke()
    except BaseException:
        _fail_authorization(authorization, approval_lifecycle)
        raise
    _complete_authorization(authorization, approval_lifecycle, audit, actor)
    return result


async def aguard_tool_call(
    action: object,
    context: object,
    invoke: Callable[[], Awaitable[Any]],
    *,
    client: ToolDecisionClient | None = None,
    unknown_side_effect: UnknownSideEffectPolicy = UnknownSideEffectPolicy.DENY,
    audit: ToolAuditSubmitter | None = None,
    actor: ActorIdentity | None = None,
    interrupt: Callable[[Mapping[str, Any]], Any] | None = None,
    approval_lifecycle: SQLiteApprovalRepository | None = None,
    invoke_with_arguments: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
    prepare_edited_arguments: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> Any:
    """Async twin of :func:`guard_tool_call` with the same completion fence."""
    authorization = _authorize_tool_action(
        action,
        context,
        client=client,
        unknown_side_effect=unknown_side_effect,
        audit=audit,
        actor=actor,
        interrupt=interrupt,
        approval_lifecycle=approval_lifecycle,
        prepare_edited_arguments=prepare_edited_arguments,
    )
    try:
        if authorization.edited:
            if invoke_with_arguments is None:
                raise ToolGovernanceError(
                    "edited tool arguments cannot be reissued on this surface"
                )
            result = await invoke_with_arguments(authorization.action.arguments)
        else:
            result = await invoke()
    except BaseException:
        _fail_authorization(authorization, approval_lifecycle)
        raise
    _complete_authorization(authorization, approval_lifecycle, audit, actor)
    return result


__all__ = [
    "TOOL_GUARD_NODE_ID",
    "ToolAuditSubmitter",
    "aguard_tool_call",
    "authorize_tool_action",
    "authorize_tool_call",
    "guard_tool_call",
]

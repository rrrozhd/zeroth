"""The one place a tool call is decided, and the order the gates close in.

**Everything that is not an explicit allow is a denial.** There is no branch
below that reads "not configured, therefore proceed": an unclassified tool is
refused, a policy source that raises is a denial, a budget backend that cannot
be reached is a denial, and an approval service that raises is a denial rather
than a fall-through to allow.

**The order is load-bearing.**

1. *Digest, then idempotency.* A key already carrying a decision re-serves it
   before anything is evaluated, so a stored deny or hold cannot be re-decided
   into an allow by a retry that happens to arrive while policy is healthy --
   nor a stored allow re-decided during an outage. A key reused for a different
   action raises instead.
2. *Side-effect gate, before any evaluator.* A tool nobody classified is refused
   without the policy evaluator ever seeing it, because a policy that waves
   through read-only calls must not be handed a call whose consequences are
   unknown. ``tests/governance/test_decision_service.py`` asserts the evaluator
   was not called, using a fresh key so an untouched evaluator cannot be
   explained by step 1 having answered first.
3. *Admission, then approval.* Only a call the admission layer admits is offered
   to the approval gate, so ``require_approval`` is reachable only from a state
   that would otherwise have allowed.

**Why admission is reused, and what reusing it does *not* buy.**
:class:`~zeroth.econ.analytics.budget.BudgetEnforcer` is fail-closed by default.
The admission combiner reached through
:class:`~zeroth.governance.decisions.admission.AdmissionEvaluator` converts a
budget checker that *raises* into a refusal (``zeroth.budget_unavailable``), and
reusing it keeps a single combiner rather than a second evaluator. Since ZER-24
that combiner is *injected* by the service domain rather than imported from it,
so governance no longer depends on the service-classified gateway package -- but
the admission path itself is unchanged and unduplicated.

But it converts only the raising path. An enforcer that catches its own
transport error and returns ``allowed=True, degraded=True,
failure_mode="fail_open"`` -- which is exactly what the shipped
explicit ``fail_closed=False`` compatibility mode does -- flows through the
combiner as a *permitted* admission. Reusing the combiner therefore does not by
itself close that opt-out. ``_evaluate`` refuses a degraded budget check as
defense in depth, and
``tests/governance/test_decision_trust_boundary.py`` pins it against the real
enforcer rather than a cooperative double.

Every reason code minted here is a member of
:data:`~zeroth.governance.audit.capture_vocabulary.REASON_CODES`; an
unregistered code is replaced by a digest at the audit capture boundary, which
is how a denial comes to look recorded while its reason is gone.
"""

from __future__ import annotations

from typing import Protocol

from zeroth.contracts.langgraph_gateway.models import AdmissionDecision, AdmissionRequest
from zeroth.governance.attestations.inventory import RegisteredTool
from zeroth.governance.audit.capture_vocabulary import REASON_CODES, normalize_reason_code
from zeroth.governance.decisions.admission import AdmissionEvaluator
from zeroth.governance.decisions.repository import DecisionRepository
from zeroth.governance.decisions.request import (
    DecisionKind,
    DecisionRequest,
    DecisionResponse,
    DecisionVerdict,
    request_digest,
)
from zeroth.governance.decisions.resolvers import (
    DeploymentPolicyResolver,
    ToolInventoryLookup,
)

_POLICY_UNAVAILABLE = "policy_unavailable"
"""Recorded when no verdict could be obtained: a raise, an outage, nonsense back."""

_POLICY_VIOLATION = "policy_violation"
"""Recorded when this module's own gate refused the call."""

_NO_REGISTERED_TERM = "unknown_error"
"""The term the two *non-denial* verdicts are recorded under.

A judgement call, and an unhappy one. :data:`REASON_CODES` enumerates *failure
and denial* names -- it holds no term meaning "permitted" and none meaning
"waiting on a human" -- and adding one is a change to a registry shared with the
audit projection, which is out of this change's scope. ``unknown_error`` is the
registry's own designated stand-in for "no registered code applies".

It is used for ``ALLOW`` **and** for ``REQUIRE_APPROVAL``. Recording a hold
under ``policy_violation`` would be the easier reach and is wrong: a call
waiting on an approver has not violated anything, and
:data:`~zeroth.governance.audit.capture_vocabulary.METADATA_VOCABULARIES`
carries ``require_approval`` as a decision term precisely so that verdict keeps
its identity. The verdict is carried by ``kind``, which is what an auditor
reads; only the reason text is impoverished, and it is impoverished rather than
actively misleading.
"""

_UNAVAILABLE_POLICY_VERSION = f"sha256:{'0' * 64}"
"""Stands in for a policy version when no policy source could supply one."""

# The admission combiner reports its outcome in the gateway's ``zeroth.*``
# error namespace; the audit vocabulary is bare snake_case. Mapped explicitly
# rather than by stripping the prefix, so a new admission reason lands on the
# conservative fallback in ``_denial_reason`` instead of inventing an
# unregistered code.
_ADMISSION_REASONS = {
    "zeroth.classifier_unavailable": "classifier_unavailable",
    "zeroth.policy_unavailable": _POLICY_UNAVAILABLE,
    "zeroth.policy_denied": _POLICY_VIOLATION,
    # An outage, not an overspend: the tenant is not over cap, the cap is
    # unknown. ``budget_exceeded`` would assert a fact nobody established.
    "zeroth.budget_unavailable": _POLICY_UNAVAILABLE,
    "zeroth.budget_denied": "budget_exceeded",
}

_SIDE_EFFECTING = "side_effecting"

_CLASSIFIED_SIDE_EFFECTS = frozenset({"read_only", _SIDE_EFFECTING})

_UNREGISTERED_TOOL = "capability_denied"
"""Recorded when the called tool is not in the deployment's registered inventory.

``capability_denied`` rather than ``policy_violation``: no policy was
consulted and none was broken. The server simply has no record that this
deployment may reach this tool, which is the closest registered term to
"capability not established".
"""


class ApprovalGate(Protocol):
    """Whatever decides that an admitted call still needs a human.

    Consulted only for calls the admission layer already admitted, so a gate
    that answers ``None`` widens nothing: it merely declines to add a hold.
    """

    async def requires_approval(
        self,
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> str | None:
        """Return the approval *request* waits on, or ``None`` if it waits on none.

        ``policy_bindings`` is the *effective* set the service resolved --
        deployment bindings unioned with the caller's -- not the raw request
        field. A gate handed the client's own bindings could be talked out of
        a hold by omitting one.
        """
        ...


class NoApprovalRequired:
    """The gate in force until a real one is injected: it adds no holds.

    Safe as a default in a way that a permissive *policy* default would not be,
    because it can only ever leave an already-admitted call admitted. It cannot
    turn a denial into an allow.
    """

    async def requires_approval(
        self,
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> str | None:
        """Add no hold: the admission layer has already decided this call."""
        del request, policy_bindings
        return None


def _denial_reason(admission: AdmissionDecision) -> str:
    """Map an admission refusal onto a registered denial term.

    Args:
        admission: The refusing admission decision.

    Returns:
        The mapped term, or a registered fallback. Never an unregistered code,
        and never a term that reads as an allow.
    """
    mapped = _ADMISSION_REASONS.get(admission.reason or "")
    if mapped is not None:
        return mapped
    normalized = normalize_reason_code(admission.reason)
    if normalized is not None and normalized in REASON_CODES:
        return normalized
    return _POLICY_VIOLATION


class ToolDecisionService:
    """Decide one tool call, fail closed, and record the decision exactly once."""

    def __init__(
        self,
        *,
        repository: DecisionRepository,
        admission_evaluator: AdmissionEvaluator,
        inventory: ToolInventoryLookup,
        deployment_policies: DeploymentPolicyResolver,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        """Wire the service.

        ``inventory`` and ``deployment_policies`` are required rather than
        optional-with-a-default on purpose. Both exist because audit round 1
        found the client supplying facts the server must own; a default would
        let a construction site silently opt back out of the fix, and the one
        default that would be safe (refuse everything) would make the service
        look broken rather than unwired.
        """
        self._repository = repository
        self._admission_evaluator = admission_evaluator
        self._inventory = inventory
        self._deployment_policies = deployment_policies
        self._approval_gate = approval_gate or NoApprovalRequired()

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        """Return the verdict for *request*, recording it exactly once.

        **The idempotency guarantee is over persistence, not evaluation.** An
        earlier revision of this docstring claimed the request was evaluated
        "at most once", and that claim is false: two concurrent *first*
        requests for one key can both miss :meth:`find_replay` and both
        evaluate policy, budget and approval. What cannot happen twice is the
        stored outcome -- ``(tenant_id, idempotency_key)`` is UNIQUE and
        :meth:`~zeroth.governance.decisions.repository.DecisionRepository.record`
        is first-write-wins, so the racers converge on one decision with one
        identity, and the loser's evaluation is discarded.

        That is safe only while evaluation is side-effect free, which it is
        here: the policy guard resolves against a registry, the budget check is
        a read, the inventory lookup is a SELECT, and
        :class:`~zeroth.governance.decisions.resolvers.PolicyApprovalGate`
        mints a *deterministic* reference from the tenant and key rather than
        creating a hold. A future gate with a real side effect -- a persisted
        approval record, a rate-limit decrement -- would need an atomic pending
        claim and a waiter protocol before this docstring could promise more.

        Args:
            request: The normalized, versioned decision request.

        Returns:
            The recorded decision. A replayed key returns the original
            decision, identity and issue time included.

        Raises:
            IdempotencyConflictError: If the key already carries a decision made
                for a materially different request.
        """
        digest = request_digest(request)
        replay = await self._repository.find_replay(request, digest)
        if replay is not None:
            return replay
        verdict = await self._evaluate(request)
        return await self._repository.record(request, digest=digest, verdict=verdict)

    async def _evaluate(self, request: DecisionRequest) -> DecisionVerdict:
        """Decide an unseen request: identity gates, then admission, then approval."""
        if request.action.side_effect not in _CLASSIFIED_SIDE_EFFECTS:
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_POLICY_VIOLATION,
                policy_version=_UNAVAILABLE_POLICY_VERSION,
            )

        if not await self._is_registered(request):
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_UNREGISTERED_TOOL,
                policy_version=_UNAVAILABLE_POLICY_VERSION,
            )

        bindings = await self._effective_bindings(request)
        if bindings is None:
            # The mandatory policy set is unknown. Passing what the client sent
            # -- or nothing -- to the evaluator would ask it to rule on a
            # policy set nobody established, and an empty set admits.
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_POLICY_UNAVAILABLE,
                policy_version=_UNAVAILABLE_POLICY_VERSION,
            )
        admission = await self._admit(self._admission_request(request, bindings))
        if admission is None:
            # The evaluator raised, or handed back something that is not an
            # admission. Either way no verdict was obtained, which is what
            # ``policy_unavailable`` records.
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_POLICY_UNAVAILABLE,
                policy_version=_UNAVAILABLE_POLICY_VERSION,
            )
        if not admission.allowed:
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_denial_reason(admission),
                policy_version=admission.policy_version,
            )
        if admission.budget_check_degraded:
            # An *allow* carrying a degraded budget check is the fail-open
            # posture, not a verdict. Explicit compatibility configuration can
            # make ``BudgetEnforcer`` answer an outage with
            # ``allowed=True, failure_mode="fail_open"``. The combiner
            # faithfully preserves that answer, which is
            # correct for run creation and wrong here: this service exists to
            # refuse what it cannot justify, and "the cap is unknown" is not a
            # justification. Checked after the refusal branch above so a
            # degraded *denial* keeps its own, more specific reason code.
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_POLICY_UNAVAILABLE,
                policy_version=admission.policy_version,
            )
        return await self._approval_verdict(request, admission, bindings)

    async def _approval_verdict(
        self,
        request: DecisionRequest,
        admission: AdmissionDecision,
        policy_bindings: tuple[str, ...],
    ) -> DecisionVerdict:
        """Turn an admitted call into an allow, or a hold, or -- on error -- a denial.

        The verdict is built *inside* the ``try`` on purpose. A gate that
        returns neither ``str`` nor ``None`` fails the frozen model's
        validation, and that failure must land on the denial branch like any
        other: outside the ``try`` it would escape ``decide`` as a
        ``ValidationError``, which is still fail-closed in effect but leaves no
        recorded denial, so nothing in the audit trail says the call was
        refused.
        """
        try:
            approval_ref = await self._approval_gate.requires_approval(
                request,
                policy_bindings,
            )
            if approval_ref is None:
                return DecisionVerdict(
                    kind=DecisionKind.ALLOW,
                    reason_code=_NO_REGISTERED_TERM,
                    policy_version=admission.policy_version,
                )
            return DecisionVerdict(
                kind=DecisionKind.REQUIRE_APPROVAL,
                reason_code=_NO_REGISTERED_TERM,
                policy_version=admission.policy_version,
                approval_ref=approval_ref,
            )
        except Exception:  # noqa: BLE001
            # A gate that cannot answer has not answered "no approval needed".
            # BaseException is deliberately not caught: cancellation is the run
            # being torn down, not a verdict.
            return DecisionVerdict(
                kind=DecisionKind.DENY,
                reason_code=_POLICY_UNAVAILABLE,
                policy_version=admission.policy_version,
            )

    async def _is_registered(self, request: DecisionRequest) -> bool:
        """Return whether this deployment registered the complete action descriptor.

        The action is resolved against inventory the *server* stored, not
        against anything in the request. A deployment that never registered an
        inventory, a lookup that fails, and an identity absent from the
        registered set are all the same answer: the server cannot say what this
        tool is, so it will not say yes to it.

        **The match covers every ``RegisteredTool`` field, exactly.** Identity
        matching prevents callable substitution, but an identity-only gate lets
        the caller weaken registered approval and classification metadata. The
        complete comparison makes the stored descriptor authoritative.

        Runs before :meth:`_admit` so an unregistered tool never reaches the policy
        evaluator -- the same ordering, and for the same reason, as the
        side-effect gate above it.
        """
        try:
            registered = await self._inventory.registered_tools(
                request.tenant_id,
                request.deployment_ref,
            )
        except Exception:  # noqa: BLE001 - an unreadable inventory is no inventory
            return False
        if registered is None:
            return False
        action = request.action
        descriptor = RegisteredTool(
            name=action.name,
            fingerprint=action.fingerprint,
            side_effect=action.side_effect,
            contract_ref=action.contract_ref,
            capability_refs=action.capability_refs,
            requires_approval=action.requires_approval,
            identity_configuration=action.identity_configuration,
        )
        return descriptor in registered

    async def _admit(self, request: AdmissionRequest) -> AdmissionDecision | None:
        """Ask the injected evaluator to rule, or report that nobody did.

        Before ZER-24 the evaluator was always ``admit``, which converted its
        own dependencies' failures into refusals. The seam is now injected, so
        neither the raise nor the return type is guaranteed any more. Both are
        collapsed to ``None`` here -- deliberately *not* to a permissive
        ``AdmissionDecision`` -- so the caller mints the denial and this method
        cannot accidentally become the thing that allows.

        The isinstance check is not defensive noise: ``object()`` is truthy, so
        a mis-wired evaluator's result would read as an allow to any caller
        that trusted the annotation and looked at ``.allowed``.

        Args:
            request: The classified admission request to rule on.

        Returns:
            The evaluator's decision, or ``None`` when no usable one was
            obtained.
        """
        try:
            decision = await self._admission_evaluator.evaluate(request)
        except Exception:  # noqa: BLE001 - an evaluator that fails admits nothing
            return None
        if not isinstance(decision, AdmissionDecision):
            return None
        return decision

    @staticmethod
    def _admission_request(
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> AdmissionRequest:
        """Project a tool decision request onto the run-shaped admission input.

        ``AdmissionRequest`` was written for run creation and carries fields a
        tool call has no answer for. Each is filled in the fail-closed
        direction: ``roles=()`` grants nothing, and ``input_payload=None`` with
        ``input_size_bytes=0`` reflects that this service is handed a digest of
        the arguments and never the arguments themselves. The tool's name rides
        on ``operation`` so a policy can be written against a specific tool.

        **The evaluated policy set is the union of the deployment's bindings
        and the caller's.** ``evaluate_run_admission`` evaluates exactly the
        bindings it is handed, so passing the client's through -- as this did
        before audit round 1 -- let a caller choose its own policies, and
        ``policy_bindings=()`` resolved none at all. Unioning means a client
        may still *add* restrictions, which can only narrow the outcome, but
        cannot drop one the server holds. A resolver that cannot answer never
        reaches this projection at all: ``_evaluate`` refuses the call before
        building an admission request, because there is no honest set to
        project. An earlier revision instead documented a resolver failure as
        "contributes no bindings", which was the fail-open path audit round 2
        flagged -- the empty set does not defer to some later unavailability
        check, it admits.
        """
        return AdmissionRequest(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            roles=(),
            deployment_ref=request.deployment_ref,
            operation=f"tool_call:{request.action.name}",
            input_payload=None,
            input_size_bytes=0,
            policy_bindings=policy_bindings,
        )

    async def _effective_bindings(self, request: DecisionRequest) -> tuple[str, ...] | None:
        """Return the server's bindings unioned with the caller's, or ``None``.

        ``None`` means the server-held set could not be determined, and is
        deliberately distinct from ``()``. A deployment that carries no
        policies is an ordinary configuration and must stay allow-capable; a
        resolver that raised has told us nothing, and the two must not collapse
        onto the same value -- which is exactly what the audited version did by
        answering ``()`` for both.
        """
        try:
            server_side = await self._deployment_policies.policy_bindings_for(
                request.tenant_id,
                request.deployment_ref,
            )
        except Exception:  # noqa: BLE001 - an unanswerable policy set is not an empty one
            return None
        return tuple(sorted({*server_side, *request.policy_bindings}))


__all__ = ["ApprovalGate", "NoApprovalRequired", "ToolDecisionService"]

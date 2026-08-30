"""Fail-closed and idempotency tests for the ZER-8 tool decision service (S4).

Each test names the requirement it pins:

* **R3** -- a replayed key with an identical action returns the *original*
  decision, same ``decision_id``.
* **R4** -- a replayed key with a different action is a conflict, not a
  silent re-answer.
* **R16** -- the idempotency key is scoped *per tenant*: the same key from two
  tenants is two records, never a collision.
* **R6** -- nothing that is not an explicit allow becomes one. An evaluator
  that raises, a budget that cannot be reached, and a replayed
  ``require_approval`` all stay non-allow.
* **R7** -- a tool nobody classified is refused *before* the policy evaluator
  is consulted, so an unclassified tool never reaches a policy that might
  allow it.

The suite carries a deliberate positive control
(``test_a_permitted_action_is_allowed_with_a_policy_version``): without it a
regression that denied everything would make every fail-closed test above pass
vacuously.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.contracts.langgraph_gateway.models import AdmissionRequest
from zeroth.econ.analytics.budget import BudgetCheckResult
from zeroth.governance.audit.capture_vocabulary import REASON_CODES
from zeroth.governance.attestations.inventory import RegisteredTool
from zeroth.governance.decisions import (
    DIGEST_EXCLUDED_FIELDS,
    DecisionKind,
    DecisionRepository,
    DecisionRequest,
    IdempotencyConflictError,
    NormalizedAction,
    ToolDecisionService,
    request_digest,
)
from zeroth.governance.policy import RunAdmissionResult
from zeroth.service.bootstrap.admission import BoundAdmissionEvaluator

POLICY_VERSION = f"sha256:{'a' * 64}"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class RecordingPolicyGuard:
    """Policy guard that answers a fixed verdict and counts every consultation."""

    def __init__(self, *, allowed: bool = True, reason: str | None = None) -> None:
        self.allowed = allowed
        self.reason = reason
        self.calls: list[AdmissionRequest] = []

    def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult:
        """Record the consultation and return the configured verdict."""
        self.calls.append(request)
        return RunAdmissionResult(
            allowed=self.allowed,
            policy_version=POLICY_VERSION,
            reason=self.reason,
        )


class RaisingPolicyGuard:
    """Policy guard standing in for an unreachable policy source."""

    def __init__(self) -> None:
        self.calls: list[AdmissionRequest] = []

    def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult:
        """Fail the way an outage fails: loudly, with no verdict."""
        self.calls.append(request)
        raise RuntimeError("policy backend unreachable")


class StubBudgetChecker:
    """Budget checker returning an under-cap posture."""

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        """Report spend well under an unlimited cap."""
        del tenant_id
        return BudgetCheckResult(allowed=True, spend_usd=1.0, cap_usd=None)


class RaisingBudgetChecker:
    """Budget checker standing in for an econ-plane outage."""

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        """Fail the way an outage fails."""
        del tenant_id
        raise RuntimeError("budget backend unreachable")


class StubApprovalGate:
    """Approval gate that always demands the named approval."""

    def __init__(self, approval_ref: str | None) -> None:
        self.approval_ref = approval_ref

    async def requires_approval(
        self,
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> str | None:
        """Return the configured approval reference."""
        del request, policy_bindings
        return self.approval_ref


class RaisingApprovalGate:
    """Approval gate standing in for an unreachable approval service."""

    async def requires_approval(
        self,
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> str | None:
        """Fail the way an outage fails."""
        del request, policy_bindings
        raise RuntimeError("approval service unreachable")


def make_action(**overrides: Any) -> NormalizedAction:
    """Build a fully classified, side-effecting normalized action."""
    fields: dict[str, Any] = {
        "name": "send_email",
        "fingerprint": f"sha256:{'1' * 64}",
        "arguments_digest": f"sha256:{'2' * 64}",
        "contract_ref": "contracts/email@v1",
        "side_effect": "side_effecting",
    }
    fields.update(overrides)
    return NormalizedAction(**fields)


def make_request(**overrides: Any) -> DecisionRequest:
    """Build a complete decision request for tenant ``alpha``."""
    fields: dict[str, Any] = {
        "tenant_id": "tenant-alpha",
        "principal_id": "principal-alpha",
        "deployment_ref": "dep-alpha",
        "action": make_action(),
        "idempotency_key": "key-alpha",
        "policy_bindings": ("binding-a",),
    }
    fields.update(overrides)
    return DecisionRequest(**fields)


class StubInventory:
    """Inventory lookup holding exactly the identity ``make_action`` produces."""

    async def registered_tools(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[RegisteredTool, ...] | None:
        """Report the complete descriptor ``make_action`` names.

        The fingerprint is read off ``make_action`` rather than repeated as a
        literal, so a change to the fixture cannot silently leave this double
        registering an identity no test actually calls.
        """
        del tenant_id, deployment_ref
        action = make_action()
        return (
            RegisteredTool(
                name=action.name,
                fingerprint=action.fingerprint,
                side_effect=action.side_effect,
                contract_ref=action.contract_ref,
                capability_refs=action.capability_refs,
                requires_approval=action.requires_approval,
                identity_configuration=action.identity_configuration,
            ),
        )


class StubDeploymentPolicies:
    """Deployment policy resolver holding no bindings of its own."""

    async def policy_bindings_for(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[str, ...]:
        """Report no server-side bindings, leaving the client's set intact."""
        del tenant_id, deployment_ref
        return ()


def make_service(
    database: Any,
    *,
    policy_guard: Any = None,
    budget_checker: Any = None,
    approval_gate: Any = None,
    inventory: Any = None,
    deployment_policies: Any = None,
) -> ToolDecisionService:
    """Wire a service over ``database`` with permissive defaults.

    The inventory defaults to one that has the fixtures' tool registered.
    Since audit round 1 an unregistered action is denied before any evaluator
    runs, so without this every test here would assert against that denial
    instead of the behaviour it names. The trust boundary itself is pinned in
    ``tests/governance/test_decision_trust_boundary.py``.
    """
    return ToolDecisionService(
        repository=DecisionRepository(database),
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=policy_guard or RecordingPolicyGuard(),
            budget_checker=budget_checker or StubBudgetChecker(),
        ),
        approval_gate=approval_gate,
        inventory=inventory or StubInventory(),
        deployment_policies=deployment_policies or StubDeploymentPolicies(),
    )


# --------------------------------------------------------------------------
# Positive control -- without this every fail-closed test below is vacuous.
# --------------------------------------------------------------------------


async def test_a_permitted_action_is_allowed_with_a_policy_version(sqlite_db: Any) -> None:
    """A classified action a permissive policy admits really comes back ALLOW."""
    service = make_service(sqlite_db)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.ALLOW
    assert response.policy_version == POLICY_VERSION
    assert response.approval_ref is None
    assert response.tenant_id == "tenant-alpha"
    assert response.reason_code in REASON_CODES


# --------------------------------------------------------------------------
# R3 / R4 / R16 -- idempotency
# --------------------------------------------------------------------------


async def test_replaying_a_key_with_the_same_action_returns_the_original(sqlite_db: Any) -> None:
    """R3: the second call re-serves the stored decision, id and all."""
    service = make_service(sqlite_db)
    request = make_request()

    first = await service.decide(request)
    second = await service.decide(request)

    assert first.decision_id == second.decision_id
    assert first == second


async def test_replaying_a_key_with_a_different_action_is_a_conflict(sqlite_db: Any) -> None:
    """R4: a key reused for another action is refused, never silently re-answered."""
    service = make_service(sqlite_db)
    await service.decide(make_request())

    with pytest.raises(IdempotencyConflictError):
        await service.decide(make_request(action=make_action(name="delete_account")))


async def test_the_same_key_in_another_tenant_is_a_separate_decision(sqlite_db: Any) -> None:
    """R16: idempotency is tenant-scoped, so a shared key is not a collision."""
    service = make_service(sqlite_db)

    alpha = await service.decide(make_request(tenant_id="tenant-alpha"))
    beta = await service.decide(make_request(tenant_id="tenant-beta"))

    assert alpha.decision_id != beta.decision_id
    assert alpha.tenant_id == "tenant-alpha"
    assert beta.tenant_id == "tenant-beta"


# --------------------------------------------------------------------------
# R6 -- an outage is never an allow
# --------------------------------------------------------------------------


async def test_a_raising_policy_evaluator_denies(sqlite_db: Any) -> None:
    """R6: a policy source that cannot answer has not answered 'allow'."""
    service = make_service(sqlite_db, policy_guard=RaisingPolicyGuard())

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_an_unreachable_budget_backend_denies(sqlite_db: Any) -> None:
    """R6: an upstream budget failure cannot become an admission allow.

    Pins the term as well as the verdict: an outage is not an overspend, so
    recording it as ``budget_exceeded`` would assert a fact nobody established.
    """
    service = make_service(sqlite_db, budget_checker=RaisingBudgetChecker())

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_an_over_cap_budget_denies(sqlite_db: Any) -> None:
    """R6: an explicit budget refusal is a denial, not a degraded allow."""

    class OverCapBudgetChecker:
        async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
            del tenant_id
            return BudgetCheckResult(allowed=False, spend_usd=99.0, cap_usd=10.0)

    service = make_service(sqlite_db, budget_checker=OverCapBudgetChecker())

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "budget_exceeded"


async def test_an_ordinary_policy_refusal_denies(sqlite_db: Any) -> None:
    """R6: the plain case -- a reachable policy that simply says no."""
    service = make_service(sqlite_db, policy_guard=RecordingPolicyGuard(allowed=False))

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_violation"
    assert response.policy_version == POLICY_VERSION


async def test_a_policy_supplied_reason_survives_when_it_is_registered(sqlite_db: Any) -> None:
    """A denial reason the audit registry knows is kept rather than flattened."""
    guard = RecordingPolicyGuard(allowed=False, reason="capability_denied")
    service = make_service(sqlite_db, policy_guard=guard)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"


async def test_an_unregistered_policy_reason_falls_back_to_a_denial_term(
    sqlite_db: Any,
) -> None:
    """An unknown reason must not reach the audit boundary as an invented code."""
    guard = RecordingPolicyGuard(allowed=False, reason="something nobody registered")
    service = make_service(sqlite_db, policy_guard=guard)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_violation"
    assert response.reason_code in REASON_CODES


async def test_an_approval_gate_returning_nonsense_denies(sqlite_db: Any) -> None:
    """R6: a gate that answers with neither a ref nor None is an outage, not an allow."""

    class NonsenseApprovalGate:
        async def requires_approval(self, request: DecisionRequest) -> Any:
            del request
            return object()

    service = make_service(sqlite_db, approval_gate=NonsenseApprovalGate())

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_a_raising_approval_gate_denies_rather_than_allows(sqlite_db: Any) -> None:
    """R6: an unreachable approval service denies; it never falls through to allow."""
    service = make_service(sqlite_db, approval_gate=RaisingApprovalGate())

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_require_approval_survives_a_replay(sqlite_db: Any) -> None:
    """R6: a held decision stays held when the same key comes back."""
    service = make_service(sqlite_db, approval_gate=StubApprovalGate("approval-1"))
    request = make_request()

    first = await service.decide(request)
    second = await service.decide(request)

    assert first.kind is DecisionKind.REQUIRE_APPROVAL
    assert first.approval_ref == "approval-1"
    assert second.kind is DecisionKind.REQUIRE_APPROVAL
    assert second.decision_id == first.decision_id
    # A hold is not a violation: recording it under a denial term would tell an
    # auditor the call broke a rule when it is merely waiting on a person.
    assert first.reason_code != "policy_violation"
    assert first.reason_code in REASON_CODES


async def test_require_approval_does_not_degrade_to_allow_during_an_outage(
    sqlite_db: Any,
) -> None:
    """R6: replaying a held decision while policy is down still returns the hold."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()

    holding = ToolDecisionService(
        repository=repository,
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=RecordingPolicyGuard(),
            budget_checker=StubBudgetChecker(),
        ),
        approval_gate=StubApprovalGate("approval-1"),
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )
    first = await holding.decide(request)
    assert first.kind is DecisionKind.REQUIRE_APPROVAL

    degraded = ToolDecisionService(
        repository=repository,
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=RaisingPolicyGuard(),
            budget_checker=RaisingBudgetChecker(),
        ),
        approval_gate=RaisingApprovalGate(),
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )
    replayed = await degraded.decide(request)

    assert replayed.kind is DecisionKind.REQUIRE_APPROVAL
    assert replayed.decision_id == first.decision_id


async def test_a_denial_is_not_upgraded_to_allow_on_retry(sqlite_db: Any) -> None:
    """R6: a stored denial is re-served even once the policy source recovers."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()

    down = ToolDecisionService(
        repository=repository,
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=RaisingPolicyGuard(),
            budget_checker=StubBudgetChecker(),
        ),
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )
    denied = await down.decide(request)
    assert denied.kind is DecisionKind.DENY

    recovered = ToolDecisionService(
        repository=repository,
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=RecordingPolicyGuard(),
            budget_checker=StubBudgetChecker(),
        ),
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )
    replayed = await recovered.decide(request)

    assert replayed.kind is DecisionKind.DENY
    assert replayed.decision_id == denied.decision_id


# --------------------------------------------------------------------------
# R2 -- the decision delegates to the shared admission combiner
# --------------------------------------------------------------------------


async def test_the_admission_request_projects_the_decision_request(sqlite_db: Any) -> None:
    """R2: what reaches the combiner is the decision request, not a re-invention.

    ``RecordingPolicyGuard`` sees the request ``admit`` derived, so this pins
    the projection end to end: tenant, principal and deployment are carried
    across unchanged, the declared bindings survive, and the tool's name rides
    on ``operation`` -- which is the only place a policy can be written against
    a specific tool.
    """
    guard = RecordingPolicyGuard()
    service = make_service(sqlite_db, policy_guard=guard)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.ALLOW
    # Exactly one consultation: a second evaluator running alongside the
    # combiner would show up here as a second recorded request.
    assert len(guard.calls) == 1
    admission = guard.calls[0]
    assert admission.tenant_id == "tenant-alpha"
    assert admission.principal_id == "principal-alpha"
    assert admission.deployment_ref == "dep-alpha"
    assert admission.policy_bindings == ("binding-a",)
    assert admission.operation == "tool_call:send_email"


async def test_the_policy_guard_is_actually_consulted_for_a_permitted_action(
    sqlite_db: Any,
) -> None:
    """R2: the mirror of the unclassified case -- a classified call *does* reach policy.

    Without this, ``test_an_unclassified_action_is_denied_before_the_evaluator_runs``
    could pass because the evaluator is never consulted at all, which would make
    every decision policy-free rather than fail-closed.
    """
    guard = RecordingPolicyGuard()
    service = make_service(sqlite_db, policy_guard=guard)

    response = await service.decide(make_request())

    assert guard.calls, "a classified action must be offered to the policy evaluator"
    assert len(guard.calls) == 1
    assert response.kind is DecisionKind.ALLOW


async def test_the_admission_request_grants_no_roles_and_carries_no_payload(
    sqlite_db: Any,
) -> None:
    """R2: the fields a tool call has no answer for are filled fail-closed.

    ``roles=()`` grants nothing, and the arguments never travel -- the service is
    handed only their digest, so a payload appearing here would be a new place a
    secret pasted into a tool call comes to rest.
    """
    guard = RecordingPolicyGuard()
    service = make_service(sqlite_db, policy_guard=guard)

    await service.decide(make_request())

    admission = guard.calls[0]
    assert admission.roles == ()
    assert admission.input_payload is None
    assert admission.input_size_bytes == 0


# --------------------------------------------------------------------------
# R7 -- unclassified tools never reach the policy evaluator
# --------------------------------------------------------------------------


async def test_an_unclassified_action_is_denied_before_the_evaluator_runs(
    sqlite_db: Any,
) -> None:
    """R7: the side-effect gate closes ahead of any policy consultation.

    The key is fresh, so an untouched evaluator cannot be explained by the
    idempotency short-circuit having answered first.
    """
    guard = RecordingPolicyGuard()
    budget = StubBudgetChecker()
    service = make_service(sqlite_db, policy_guard=guard, budget_checker=budget)

    response = await service.decide(
        make_request(
            idempotency_key="key-never-used-before",
            action=make_action(side_effect="unknown"),
        )
    )

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_violation"
    assert guard.calls == [], "the policy evaluator must not see an unclassified tool"


async def test_an_unclassified_denial_is_still_recorded(sqlite_db: Any) -> None:
    """R7: refusing early must not skip persistence -- the denial is evidence."""
    repository = DecisionRepository(sqlite_db)
    service = ToolDecisionService(
        repository=repository,
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=RecordingPolicyGuard(),
            budget_checker=StubBudgetChecker(),
        ),
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )
    request = make_request(action=make_action(side_effect="unknown"))

    response = await service.decide(request)
    stored = await repository.find_by_idempotency_key("tenant-alpha", "key-alpha")

    assert stored is not None
    assert stored.response.decision_id == response.decision_id
    assert stored.response.kind is DecisionKind.DENY


# --------------------------------------------------------------------------
# request_digest coverage
# --------------------------------------------------------------------------

# Every mutable field of the request, with a value that differs from the one
# ``make_request`` uses. The set-equality assertion below is what makes adding a
# field to ``DecisionRequest`` without thinking about the digest a test failure.
REQUEST_MUTATIONS: dict[str, Any] = {
    "tenant_id": "tenant-other",
    "principal_id": "principal-other",
    "deployment_ref": "dep-other",
    "action": make_action(name="other_tool"),
    "policy_bindings": ("binding-b",),
}

ACTION_MUTATIONS: dict[str, Any] = {
    "name": "other_tool",
    "fingerprint": f"sha256:{'9' * 64}",
    "arguments_digest": f"sha256:{'8' * 64}",
    "contract_ref": "contracts/other@v9",
    "side_effect": "read_only",
    "capability_refs": ("network_write",),
    "requires_approval": True,
    "identity_configuration": ("endpoint",),
}


def test_the_digest_excludes_only_the_idempotency_key() -> None:
    """The exclusion set is the whole of what escapes the digest.

    ``schema_version`` cannot be mutation-tested because ``Literal[1]`` admits
    exactly one value, so this assertion is what proves it is still covered:
    anything added to the exclusion set fails here.
    """
    assert set(DIGEST_EXCLUDED_FIELDS) == {"idempotency_key"}


def test_every_covered_request_field_is_exercised() -> None:
    """A new request field must be given a mutation or the digest test is a lie."""
    covered = set(DecisionRequest.model_fields) - DIGEST_EXCLUDED_FIELDS - {"schema_version"}
    assert set(REQUEST_MUTATIONS) == covered


def test_every_covered_action_field_is_exercised() -> None:
    """A new action field must be given a mutation too."""
    assert set(ACTION_MUTATIONS) == set(NormalizedAction.model_fields)


@pytest.mark.parametrize("field", sorted(REQUEST_MUTATIONS))
def test_changing_any_request_field_changes_the_digest(field: str) -> None:
    """Identity, principal, tenant, deployment and bindings all bind the digest."""
    baseline = request_digest(make_request())
    mutated = request_digest(make_request(**{field: REQUEST_MUTATIONS[field]}))

    assert mutated != baseline


@pytest.mark.parametrize("field", sorted(ACTION_MUTATIONS))
def test_changing_any_action_field_changes_the_digest(field: str) -> None:
    """The action is digested field by field, not by name alone."""
    baseline = request_digest(make_request())
    mutated = request_digest(make_request(action=make_action(**{field: ACTION_MUTATIONS[field]})))

    assert mutated != baseline


def test_the_idempotency_key_itself_is_outside_the_digest() -> None:
    """Two keys over one action must agree, or every replay would look changed."""
    first = request_digest(make_request(idempotency_key="key-one"))
    second = request_digest(make_request(idempotency_key="key-two"))

    assert first == second

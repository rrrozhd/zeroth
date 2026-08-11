"""Trust-boundary acceptance tests for the ZER-8 tool decision service.

These pin properties audit round 1 proved were *not* held. Each drives the real
collaborator rather than a cooperative double, because the defect in every case
lived in the difference between the two.

* **R6** -- a budget *outage* cannot become an allow. The audit's finding was
  that the existing R6 tests only covered the path where the checker
  ``raise``s; the real :class:`~zeroth.econ.analytics.budget.BudgetEnforcer`
  defaults to fail-*open* and answers an outage with ``allowed=True`` and
  ``failure_mode="fail_open"``, which ``admit`` faithfully preserves and the
  service converted into ALLOW. So the test here drives the real enforcer with
  a transport that fails, never a fake reporting ``degraded=True`` -- a fake
  would prove nothing about the posture that actually ships.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.governance.test_decision_service import (
    RecordingPolicyGuard,
    StubBudgetChecker,
    make_action,
    make_request,
)
from zeroth.econ.analytics.budget import BudgetEnforcer
from zeroth.governance.attestations.inventory import RegisteredTool
from zeroth.governance.attestations.store import (
    InventoryRegistration,
    InventoryRegistrationRepository,
)
from zeroth.governance.decisions import (
    DecisionKind,
    DecisionRepository,
    ToolDecisionService,
)
from zeroth.governance.decisions.resolvers import (
    DeploymentRecordPolicyResolver,
    PolicyApprovalGate,
    RegisteredInventoryLookup,
)
from zeroth.governance.policy.models import PolicyDefinition
from zeroth.service.bootstrap.admission import BoundAdmissionEvaluator


def _unreachable_budget_backend(request: httpx.Request) -> httpx.Response:
    """Fail the way an unreachable econ plane fails."""
    raise httpx.ConnectError("econ plane unreachable", request=request)


class StaticInventory:
    """Inventory lookup answering with one fixed set of registered identities.

    Constructed from names for brevity; each is paired with the fingerprint
    ``make_action`` produces, so the default set registers exactly the identity
    the default request calls. A test that wants a *mismatched* fingerprint
    varies the action, not this double -- keeping the registered side fixed is
    what makes the fingerprint the only moving part.
    """

    def __init__(
        self,
        names: tuple[str, ...] | None = ("send_email",),
        *,
        action: Any = None,
    ) -> None:
        self._names = names
        self._action = make_action() if action is None else action
        self.calls: list[tuple[str, str]] = []

    async def registered_tools(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[RegisteredTool, ...] | None:
        """Return complete registered descriptors, or ``None``."""
        self.calls.append((tenant_id, deployment_ref))
        if self._names is None:
            return None
        return tuple(_registered_tool(name=name, action=self._action) for name in self._names)


class StaticDeploymentPolicies:
    """Deployment policy resolver answering with one fixed binding set."""

    def __init__(self, bindings: tuple[str, ...] = ()) -> None:
        self._bindings = bindings

    async def policy_bindings_for(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[str, ...]:
        """Return the bindings the server holds for this deployment."""
        del tenant_id, deployment_ref
        return self._bindings


def make_guarded_service(
    database: Any,
    *,
    policy_guard: Any = None,
    budget_checker: Any = None,
    approval_gate: Any = None,
    inventory: Any = None,
    deployment_policies: Any = None,
) -> ToolDecisionService:
    """Wire a service whose inventory and deployment policies are explicit."""
    return ToolDecisionService(
        repository=DecisionRepository(database),
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=policy_guard or RecordingPolicyGuard(),
            budget_checker=budget_checker or StubBudgetChecker(),
        ),
        approval_gate=approval_gate,
        inventory=inventory if inventory is not None else StaticInventory(),
        deployment_policies=(
            deployment_policies if deployment_policies is not None else StaticDeploymentPolicies()
        ),
    )


def make_service(database: Any, *, budget_checker: Any = None) -> ToolDecisionService:
    """Wire a decision service over *database* with a permissive policy guard."""
    return make_guarded_service(database, budget_checker=budget_checker)


async def test_a_fail_open_budget_outage_denies_rather_than_allowing(
    sqlite_db: Any,
) -> None:
    """R6: the real fail-open enforcer's outage posture is refused here.

    The enforcer is constructed as production constructs it -- ``fail_closed``
    left at its shipped default of ``False`` -- so this asserts against the
    posture that actually runs. Its outage answer is ``allowed=True,
    degraded=True, failure_mode="fail_open"``; a service reading only
    ``allowed`` returns ALLOW, which is the audited defect.
    """
    enforcer = BudgetEnforcer(_transport=_unreachable_budget_backend)
    outage = await enforcer.check_budget_status("tenant-alpha")
    assert outage.allowed is True, "the shipped enforcer must still fail open"
    assert outage.failure_mode == "fail_open"

    service = make_service(sqlite_db, budget_checker=enforcer)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_a_healthy_budget_still_allows(sqlite_db: Any) -> None:
    """The positive control: the degraded gate must not deny a healthy check.

    Without this, a regression denying every decision would satisfy the test
    above vacuously.
    """
    service = make_service(sqlite_db)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.ALLOW


# --------------------------------------------------------------------------
# R6 -- an approval-required policy holds instead of allowing
# --------------------------------------------------------------------------


class ApprovalRequiringRegistry:
    """Policy registry whose one policy demands approval for side effects.

    Stands in for the registry ``PolicyGuard`` resolves against, carrying the
    real ``PolicyDefinition`` field rather than a bespoke flag, so the gate is
    exercised against the attribute production reads.
    """

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> PolicyDefinition:
        """Return a policy requiring sign-off before any side effect."""
        self.resolved.append(ref)
        return PolicyDefinition(
            policy_id=ref,
            approval_required_for_side_effects=True,
        )


async def test_an_approval_required_policy_is_not_allowed(sqlite_db: Any) -> None:
    """R6: a side-effecting call under an approval policy is held, not allowed.

    No approval gate is injected, so this exercises the default the factory
    used to ship. The audited defect was that the default gate
    (``NoApprovalRequired``) always answered "no hold", and the run-admission
    result's ``approval_required_for_side_effects`` was never consulted at all.
    """
    service = make_guarded_service(
        sqlite_db,
        approval_gate=PolicyApprovalGate(ApprovalRequiringRegistry()),
        deployment_policies=StaticDeploymentPolicies(("needs-approval",)),
    )

    response = await service.decide(make_request(policy_bindings=()))

    assert response.kind is DecisionKind.REQUIRE_APPROVAL
    assert response.approval_ref is not None


async def test_a_read_only_call_is_not_held_by_a_side_effect_approval_policy(
    sqlite_db: Any,
) -> None:
    """The hold is scoped to side effects: a read-only call still passes.

    The positive control for the test above -- without it, a gate that held
    everything would satisfy the approval assertion vacuously.
    """
    service = make_guarded_service(
        sqlite_db,
        approval_gate=PolicyApprovalGate(ApprovalRequiringRegistry()),
        inventory=StaticInventory(action=make_action(side_effect="read_only")),
        deployment_policies=StaticDeploymentPolicies(("needs-approval",)),
    )

    response = await service.decide(
        make_request(action=make_action(side_effect="read_only"), policy_bindings=())
    )

    assert response.kind is DecisionKind.ALLOW


async def test_a_tool_explicitly_requiring_approval_is_held_even_when_read_only(
    sqlite_db: Any,
) -> None:
    """A tool-level approval declaration is authoritative, not merely metadata."""
    registry = ApprovalRequiringRegistry()
    service = make_guarded_service(
        sqlite_db,
        approval_gate=PolicyApprovalGate(registry),
        inventory=StaticInventory(
            action=make_action(side_effect="read_only", requires_approval=True)
        ),
        deployment_policies=StaticDeploymentPolicies(()),
    )

    response = await service.decide(
        make_request(
            action=make_action(side_effect="read_only", requires_approval=True),
            policy_bindings=(),
        )
    )

    assert response.kind is DecisionKind.REQUIRE_APPROVAL
    assert response.approval_ref is not None
    assert registry.resolved == []


# --------------------------------------------------------------------------
# R7 -- an action outside the registered inventory is refused
# --------------------------------------------------------------------------


async def test_an_unregistered_action_is_not_allowed_by_default(sqlite_db: Any) -> None:
    """R7: a tool the deployment never registered is denied before evaluation."""
    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        inventory=StaticInventory(("send_email",)),
    )

    response = await service.decide(make_request(action=make_action(name="exfiltrate_secrets")))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"
    assert guard.calls == [], "an unregistered tool must never reach the evaluator"


async def test_a_deployment_with_no_registered_inventory_allows_nothing(
    sqlite_db: Any,
) -> None:
    """R7: absence of a registration is refusal, not an unrestricted default."""
    service = make_guarded_service(sqlite_db, inventory=StaticInventory(None))

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"


# --------------------------------------------------------------------------
# R2 -- the client cannot choose its own policy set
# --------------------------------------------------------------------------


async def test_server_derived_policy_bindings_are_not_client_removable(
    sqlite_db: Any,
) -> None:
    """R2: the deployment's bindings are evaluated whatever the client sends.

    The audited defect: ``evaluate_run_admission`` evaluates *only*
    ``request.policy_bindings``, and the projection passed the client's
    straight through -- so ``policy_bindings=()`` resolved zero policies and
    was allowed. Here the client sends an empty tuple and the deployment's
    binding must still reach the evaluator.
    """
    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        deployment_policies=StaticDeploymentPolicies(("deployment-required",)),
    )

    await service.decide(make_request(policy_bindings=()))

    assert len(guard.calls) == 1
    assert "deployment-required" in guard.calls[0].policy_bindings


class UnavailableDeploymentPolicies:
    """Deployment policy resolver that cannot answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def policy_bindings_for(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[str, ...]:
        """Fail the way an unreachable deployment store fails."""
        del tenant_id, deployment_ref
        self.calls += 1
        raise RuntimeError("deployment store unreachable")


async def test_an_unavailable_policy_resolver_denies_rather_than_evaluating_nothing(
    sqlite_db: Any,
) -> None:
    """R2: a resolver that cannot answer is an outage, not an empty policy set.

    The audited defect: the resolver's exception was caught and turned into
    ``server_side=()``. Unioned with a client that sends no bindings of its own,
    the evaluator was then handed the empty set -- and evaluating zero policies
    admits. So the one failure mode where the server cannot tell whether the
    call is permitted resolved to *allow*, which is the fail-open shape the
    whole module claims not to have.

    The mandatory policy set being unknown is exactly the state
    ``policy_unavailable`` names.
    """
    guard = RecordingPolicyGuard()
    resolver = UnavailableDeploymentPolicies()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        deployment_policies=resolver,
    )

    response = await service.decide(make_request(policy_bindings=()))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"
    assert resolver.calls == 1, "the resolver must actually have been consulted"
    assert guard.calls == [], "an unknown policy set must not reach the evaluator"


async def test_an_unavailable_policy_resolver_denies_even_with_client_bindings(
    sqlite_db: Any,
) -> None:
    """R2: the caller's own bindings cannot substitute for the server's.

    Without this the fix could be read as "deny when the effective set is
    empty", which a client could satisfy by sending a harmless binding of its
    own and so buy back the allow the outage should have cost it.
    """
    service = make_guarded_service(
        sqlite_db,
        deployment_policies=UnavailableDeploymentPolicies(),
    )

    response = await service.decide(make_request(policy_bindings=("client-extra",)))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"


async def test_a_deployment_holding_no_bindings_still_allows(sqlite_db: Any) -> None:
    """The positive control: an *empty* server set is a config state, not a fault.

    A deployment that carries no policies is a legitimate configuration and
    must remain allow-capable. Only a resolver that could not answer denies --
    which is why the fix distinguishes "no bindings" from "no answer" rather
    than treating an empty tuple as failure.
    """
    service = make_guarded_service(
        sqlite_db,
        deployment_policies=StaticDeploymentPolicies(()),
    )

    response = await service.decide(make_request(policy_bindings=()))

    assert response.kind is DecisionKind.ALLOW


async def test_client_policy_bindings_are_added_to_the_server_set(
    sqlite_db: Any,
) -> None:
    """R2: a client may *add* restrictions -- both binding sets are evaluated."""
    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        deployment_policies=StaticDeploymentPolicies(("deployment-required",)),
    )

    await service.decide(make_request(policy_bindings=("client-extra",)))

    assert len(guard.calls) == 1
    bindings = set(guard.calls[0].policy_bindings)
    assert {"deployment-required", "client-extra"} <= bindings


async def test_a_registered_name_with_a_foreign_fingerprint_is_not_allowed(
    sqlite_db: Any,
) -> None:
    """R7: admission is by identity, not by label.

    The audited defect: ``_is_registered`` compared ``action.name`` alone,
    while both sides already carried fingerprints. A call arriving as the
    registered name ``send_email`` but fingerprinted ``fp-IMPOSTOR`` -- a
    different callable answering to a governed name, which is precisely the
    substitution ZER-6's suite exists to detect -- was admitted and allowed.

    The inventory here is the default one, whose single identity is exactly
    what ``make_action`` produces, so the *only* difference between this
    request and the allowed control below is the fingerprint.
    """
    guard = RecordingPolicyGuard()
    service = make_guarded_service(sqlite_db, policy_guard=guard)

    response = await service.decide(make_request(action=make_action(fingerprint="fp-IMPOSTOR")))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"
    assert guard.calls == [], "a substituted tool must never reach the evaluator"


async def test_the_registered_identity_itself_is_allowed(sqlite_db: Any) -> None:
    """R7: the positive control for identity matching.

    Paired with the test above deliberately. ``_is_registered`` treats any
    lookup failure as "not registered", so a double whose method the service no
    longer calls would satisfy the DENY assertion for entirely the wrong reason
    -- the vacuity audit round 2 found in the attestation-provider tests. This
    half fails loudly if the lookup is never reached.
    """
    service = make_guarded_service(sqlite_db)

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.ALLOW


@pytest.mark.parametrize("side_effect", ["read_only", "side_effecting"])
async def test_the_registered_inventory_is_consulted_for_every_classification(
    sqlite_db: Any,
    side_effect: str,
) -> None:
    """R7: the inventory gate is not skipped for read-only calls."""
    inventory = StaticInventory(("send_email",))
    service = make_guarded_service(sqlite_db, inventory=inventory)

    await service.decide(make_request(action=make_action(side_effect=side_effect)))

    assert inventory.calls == [("tenant-alpha", "dep-alpha")]


# --------------------------------------------------------------------------
# The PRODUCTION resolvers, not protocol doubles
# --------------------------------------------------------------------------
#
# Every test above drives ``StaticInventory`` and ``StaticDeploymentPolicies``.
# Those pin what the *service* does with an answer, and nothing at all about
# the two classes that actually produce it in production --
# ``RegisteredInventoryLookup`` and ``DeploymentRecordPolicyResolver``
# (``governance/decisions/resolvers.py``, wired at
# ``service/bootstrap/factory.py``). Gutting either left the suite green, which
# is the gap audit round 3 found. The tests below instantiate the concrete
# classes.


REGISTERED_FINGERPRINT = make_action().fingerprint
"""The fingerprint the default request calls, read off the fixture."""


def _registered_tool(
    *, name: str = "send_email", action: Any = None, **overrides: Any
) -> RegisteredTool:
    """Mirror one normalized action into the authoritative registration model."""
    source = make_action() if action is None else action
    values = {
        "name": name,
        "fingerprint": source.fingerprint,
        "side_effect": source.side_effect,
        "contract_ref": source.contract_ref,
        "capability_refs": source.capability_refs,
        "requires_approval": source.requires_approval,
        "identity_configuration": source.identity_configuration,
    }
    values.update(overrides)
    return RegisteredTool(**values)


async def _register_inventory(
    database: Any,
    tools: tuple[RegisteredTool, ...],
) -> InventoryRegistrationRepository:
    """Store one real registration for ``tenant-alpha`` / ``dep-alpha``."""
    repository = InventoryRegistrationRepository(database)
    await repository.register(
        InventoryRegistration(
            tenant_id="tenant-alpha",
            deployment_ref="dep-alpha",
            graph_version="graph-v1",
            adapter_version="0.1.0",
            coverage="complete",
            tools=tools,
        )
    )
    return repository


async def test_the_production_inventory_lookup_admits_the_registered_identity(
    sqlite_db: Any,
) -> None:
    """The real lookup over a real registration allows the registered call.

    This is the half that fails if ``registered_tools`` goes back to returning
    bare names: the service matches complete descriptors, so a set of plain
    strings matches nothing and the registered call is denied.
    Driving the whole service rather than asserting on the returned set is
    deliberate -- an assertion on the frozenset alone would also be satisfied
    by a mutation that returned pairs of the wrong thing.
    """
    repository = await _register_inventory(
        sqlite_db,
        (_registered_tool(),),
    )
    service = make_guarded_service(
        sqlite_db,
        inventory=RegisteredInventoryLookup(repository),
    )

    response = await service.decide(make_request())

    assert response.kind is DecisionKind.ALLOW


async def test_the_production_inventory_lookup_denies_an_impostor_fingerprint(
    sqlite_db: Any,
) -> None:
    """The real lookup carries the fingerprint through, so a substitution is denied.

    Paired with the test above: the registration and the request agree on the
    name ``send_email`` and differ only in the fingerprint. A lookup that
    discarded the fingerprint would admit this.
    """
    repository = await _register_inventory(
        sqlite_db,
        (_registered_tool(),),
    )
    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        inventory=RegisteredInventoryLookup(repository),
    )

    response = await service.decide(make_request(action=make_action(fingerprint="fp-IMPOSTOR")))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"
    assert guard.calls == [], "a substituted tool must never reach the evaluator"


async def test_registered_approval_metadata_cannot_be_downgraded_by_the_request(
    sqlite_db: Any,
) -> None:
    """A request cannot weaken the complete descriptor stored at registration."""
    repository = await _register_inventory(
        sqlite_db,
        (
            _registered_tool(
                side_effect="read_only",
                requires_approval=True,
            ),
        ),
    )
    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        inventory=RegisteredInventoryLookup(repository),
    )

    response = await service.decide(
        make_request(action=make_action(side_effect="read_only", requires_approval=False))
    )

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"
    assert guard.calls == [], "a descriptor mismatch must not reach policy evaluation"


async def test_the_production_inventory_lookup_reports_a_never_registered_deployment(
    sqlite_db: Any,
) -> None:
    """No registration reads as ``None``, which refuses everything.

    ``None`` and an empty frozenset both refuse, so the service-level assertion
    cannot distinguish them; the return value is checked directly as well, because
    the distinction is what the class exists to preserve.
    """
    lookup = RegisteredInventoryLookup(InventoryRegistrationRepository(sqlite_db))

    assert await lookup.registered_tools("tenant-alpha", "dep-alpha") is None

    service = make_guarded_service(sqlite_db, inventory=lookup)
    response = await service.decide(make_request())

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "capability_denied"


class _Deployment:
    """The only thing ``DeploymentRecordPolicyResolver`` reads off a deployment."""

    def __init__(self, policy_bindings: tuple[str, ...]) -> None:
        self.policy_bindings = policy_bindings


async def test_the_production_policy_resolver_denies_an_unloadable_deployment(
    sqlite_db: Any,
) -> None:
    """A deployment that does not resolve is ``policy_unavailable``, not empty bindings.

    **What is under test is the resolver's own branch**, not the fetcher. The
    fetcher is a seam by construction: ``zeroth.governance`` may not import the
    service layer, so the deployment record necessarily arrives through an
    injected coroutine. The branch on ``None`` -- raise rather than ``return
    ()`` -- lives in the production class and is what this drives.

    The client sends no bindings of its own, deliberately: with a client
    binding present the union is non-empty and a resolver that had gone back to
    returning ``()`` would still reach the evaluator and allow, so the mutation
    would survive.
    """

    async def _missing(deployment_ref: str) -> Any:
        del deployment_ref
        return None

    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        deployment_policies=DeploymentRecordPolicyResolver(_missing),
    )

    response = await service.decide(make_request(policy_bindings=()))

    assert response.kind is DecisionKind.DENY
    assert response.reason_code == "policy_unavailable"
    assert guard.calls == [], "an unknown policy set must not reach the evaluator"


async def test_the_production_policy_resolver_allows_a_deployment_holding_no_bindings(
    sqlite_db: Any,
) -> None:
    """The positive control: an *empty* binding set is a config state, not a fault.

    Without this, a resolver that raised unconditionally would satisfy the test
    above while denying every legitimately unbound deployment.
    """

    async def _unbound(deployment_ref: str) -> Any:
        del deployment_ref
        return _Deployment(())

    service = make_guarded_service(
        sqlite_db,
        deployment_policies=DeploymentRecordPolicyResolver(_unbound),
    )

    response = await service.decide(make_request(policy_bindings=()))

    assert response.kind is DecisionKind.ALLOW


async def test_the_production_policy_resolver_passes_the_deployments_bindings_through(
    sqlite_db: Any,
) -> None:
    """The server's own bindings reach the evaluator via the real resolver.

    Pins that the class reads ``policy_bindings`` off the record rather than
    answering with a constant -- which both tests above would tolerate.
    """

    async def _bound(deployment_ref: str) -> Any:
        del deployment_ref
        return _Deployment(("deployment-required",))

    guard = RecordingPolicyGuard()
    service = make_guarded_service(
        sqlite_db,
        policy_guard=guard,
        deployment_policies=DeploymentRecordPolicyResolver(_bound),
    )

    await service.decide(make_request(policy_bindings=()))

    assert len(guard.calls) == 1
    assert "deployment-required" in guard.calls[0].policy_bindings

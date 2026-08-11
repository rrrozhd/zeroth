"""The governance-owned admission seam ``ToolDecisionService`` depends on (ZER-24).

Before ZER-24 the service imported ``admit``, ``PolicyAdmissionChecker`` and
``BudgetChecker`` straight out of the service-classified gateway package. That
edge was the whole reason temporary exception E1 existed: governance may depend
only on contracts and platform.

The inversion replaces those three imports with
:class:`~zeroth.governance.decisions.admission.AdmissionEvaluator`, a protocol
governance owns. The service domain supplies the implementation that binds the
existing ``admit`` combiner, so the admission *logic* is still shared -- there is
no second combiner -- while the *dependency* now points from service into
governance rather than the other way round.

What that buys, and what it costs, is the subject of this suite:

* the seam is honoured -- a permitted admission still reaches an allow, so the
  fail-closed tests below cannot pass vacuously;
* the service now accepts an evaluator it does not control, so it must defend
  against one that raises or returns nonsense. ``admit`` used to be the only
  possible implementation and did that defending itself. Both paths deny, and
  deny under a *registered* reason code.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.contracts.langgraph_gateway.models import AdmissionDecision, AdmissionRequest
from zeroth.governance.audit.capture_vocabulary import REASON_CODES
from zeroth.governance.attestations.inventory import RegisteredTool
from zeroth.governance.decisions import (
    DecisionKind,
    DecisionRepository,
    DecisionRequest,
    NormalizedAction,
    ToolDecisionService,
)
from zeroth.governance.decisions.admission import AdmissionEvaluator

POLICY_VERSION = f"sha256:{'a' * 64}"

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class AllowingEvaluator:
    """Admit everything, recording what it was asked to rule on."""

    def __init__(self) -> None:
        self.seen: list[AdmissionRequest] = []

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Admit ``request`` and keep it for inspection."""
        self.seen.append(request)
        return AdmissionDecision(allowed=True, policy_version=POLICY_VERSION)


class RaisingEvaluator:
    """An evaluator whose backend is down."""

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Fail the way a broken dependency fails: loudly."""
        del request
        raise RuntimeError("admission backend unreachable")


class NonsenseEvaluator:
    """An evaluator returning something that is not an ``AdmissionDecision``.

    The realistic shape of this bug is a coroutine wired to the wrong callable,
    or a double that forgot to model the return type -- not an attacker. It is
    dangerous precisely because ``object()`` is truthy, so a service reading
    ``.allowed`` off it with ``getattr(..., True)`` would allow.
    """

    async def evaluate(self, request: AdmissionRequest) -> Any:
        """Return a truthy object with no admission fields at all."""
        del request
        return object()


class DegradedBudgetEvaluator:
    """Admit, but report that the budget check could not be trusted."""

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Mirror ``BudgetEnforcer``'s shipped fail-*open* outage answer."""
        del request
        return AdmissionDecision(
            allowed=True,
            policy_version=POLICY_VERSION,
            budget_check_degraded=True,
        )


class RefusingEvaluator:
    """Refuse under a named gateway reason."""

    def __init__(self, reason: str = "zeroth.policy_denied") -> None:
        self._reason = reason

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Refuse ``request`` with the configured reason."""
        del request
        return AdmissionDecision(
            allowed=False,
            policy_version=POLICY_VERSION,
            reason=self._reason,
        )


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
        """Report the complete descriptor ``make_action`` names."""
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


def make_service(database: Any, evaluator: Any) -> ToolDecisionService:
    """Wire a service over ``database`` around ``evaluator``."""
    return ToolDecisionService(
        repository=DecisionRepository(database),
        admission_evaluator=evaluator,
        inventory=StubInventory(),
        deployment_policies=StubDeploymentPolicies(),
    )


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


def test_the_admission_evaluator_protocol_is_owned_by_governance() -> None:
    """The seam lives in governance, so depending on it is a legal edge.

    Asserted on the protocol's own ``__module__`` rather than on the import
    statement above: an ``AdmissionEvaluator`` re-exported from governance but
    *defined* in the service package would satisfy the import and still be the
    forbidden dependency E1 named.
    """
    assert AdmissionEvaluator.__module__.startswith("zeroth.governance.")


async def test_a_permitted_action_is_allowed_through_the_injected_evaluator(
    sqlite_db: Any,
) -> None:
    """Positive control: without it every denial test below is vacuous."""
    evaluator = AllowingEvaluator()
    service = make_service(sqlite_db, evaluator)

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.ALLOW
    assert verdict.policy_version == POLICY_VERSION


async def test_the_injected_evaluator_is_actually_consulted(sqlite_db: Any) -> None:
    """The service delegates rather than deciding admission by itself.

    A service that allowed without ever calling the evaluator would pass the
    positive control above, so the call itself is pinned separately.
    """
    evaluator = AllowingEvaluator()
    service = make_service(sqlite_db, evaluator)

    await service.decide(make_request())

    assert len(evaluator.seen) == 1
    assert isinstance(evaluator.seen[0], AdmissionRequest)
    assert evaluator.seen[0].tenant_id == "tenant-alpha"


# --------------------------------------------------------------------------
# Fail-closed: the service no longer controls the evaluator's implementation
# --------------------------------------------------------------------------


async def test_a_raising_evaluator_denies(sqlite_db: Any) -> None:
    """An evaluator that raises is a denial, never an allow.

    ``admit`` converted its own dependencies' failures into refusals. Now that
    the whole evaluator is injected, nothing upstream guarantees that, so the
    service has to catch it here.
    """
    service = make_service(sqlite_db, RaisingEvaluator())

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code in REASON_CODES


async def test_an_evaluator_returning_nonsense_denies(sqlite_db: Any) -> None:
    """A non-``AdmissionDecision`` result is a denial under a registered code."""
    service = make_service(sqlite_db, NonsenseEvaluator())

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code in REASON_CODES


async def test_a_degraded_budget_check_is_refused_rather_than_allowed(
    sqlite_db: Any,
) -> None:
    """The explicit degraded-budget refusal survives the inversion.

    This is the behaviour the module docstring calls out as *not* inherited
    from ``admit``: a fail-open outage answer arrives as ``allowed=True`` and
    must still be refused here.
    """
    service = make_service(sqlite_db, DegradedBudgetEvaluator())

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code == "policy_unavailable"


async def test_an_explicit_refusal_keeps_its_mapped_reason(sqlite_db: Any) -> None:
    """A refusal maps onto the registered denial term, not a generic one."""
    service = make_service(sqlite_db, RefusingEvaluator("zeroth.policy_denied"))

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code == "policy_violation"


async def test_an_over_cap_refusal_is_reported_as_a_budget_denial(
    sqlite_db: Any,
) -> None:
    """Over-cap keeps its own term rather than collapsing into a generic denial."""
    service = make_service(sqlite_db, RefusingEvaluator("zeroth.budget_denied"))

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code == "budget_exceeded"


async def test_a_budget_outage_refusal_is_not_reported_as_an_overspend(
    sqlite_db: Any,
) -> None:
    """An unknown cap is an outage, not an overspend.

    ``budget_exceeded`` would assert a fact nobody established -- the tenant is
    not over cap, the cap could not be read.
    """
    service = make_service(sqlite_db, RefusingEvaluator("zeroth.budget_unavailable"))

    verdict = await service.decide(make_request())

    assert verdict.kind is DecisionKind.DENY
    assert verdict.reason_code == "policy_unavailable"


async def test_the_service_requires_an_admission_evaluator(sqlite_db: Any) -> None:
    """The dependency is required, not optional-with-a-default.

    A default would let a construction site silently opt back out of the
    inversion, and the only safe default -- refuse everything -- would make the
    service look broken rather than unwired.
    """
    with pytest.raises(TypeError):
        ToolDecisionService(  # type: ignore[call-arg]
            repository=DecisionRepository(sqlite_db),
            inventory=StubInventory(),
            deployment_policies=StubDeploymentPolicies(),
        )

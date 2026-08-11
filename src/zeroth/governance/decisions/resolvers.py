"""Server-held answers to the two questions a tool decision must not ask a client.

Audit round 1 found that :class:`~zeroth.governance.decisions.service.ToolDecisionService`
took both the set of policies to evaluate and the identity of the tool being
called from the caller's own request. An arbitrary, unregistered action
submitted with ``policy_bindings=()`` therefore resolved zero policies and was
allowed. These resolvers are where those two facts come from instead.

Both are expressed against callables rather than concrete stores so that
``zeroth.governance`` keeps its permitted dependencies (``contracts`` and
``platform``). The deployment record lives behind the service layer, which
governance may not import; a caller there supplies the lookup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from zeroth.governance.attestations.inventory import RegisteredTool
from zeroth.governance.decisions.request import DecisionRequest


class ToolInventoryLookup(Protocol):
    """The complete tool descriptors a deployment has registered.

    Complete descriptors, not names or identity pairs. Identity-only matching
    prevents callable substitution but still lets a request weaken approval,
    side-effect, contract, capability, or identity-configuration facts the
    deployment registered. Admission therefore compares the entire immutable
    server-held descriptor.
    """

    async def registered_tools(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[RegisteredTool, ...] | None:
        """Return the registered governance descriptors, or ``None``.

        ``None`` means no inventory was ever registered, which is distinct
        from an empty set: both refuse everything, for different reasons.
        """
        ...


class DeploymentPolicyResolver(Protocol):
    """The policy bindings a deployment carries on the server."""

    async def policy_bindings_for(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[str, ...]:
        """Return the server-held bindings for this deployment."""
        ...


class RegisteredInventoryLookup:
    """Answer from the deployment's most recent inventory registration.

    ``None`` -- never registered -- is deliberately distinct from an empty
    tuple. The first means the server knows nothing about this
    deployment's tools and must refuse everything; the second means the
    deployment registered that it governs no tools, which refuses everything
    too but for a reason an operator can act on.
    """

    def __init__(self, registrations: Any) -> None:
        self._registrations = registrations

    async def registered_tools(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[RegisteredTool, ...] | None:
        """Return complete descriptors from the newest registration.

        ``RegisteredTool`` carries every governance field and the registration
        digest covers them all. Returning the models whole keeps those stored
        facts authoritative at admission rather than projecting them away.
        """
        registration = await self._registrations.latest_for_deployment(
            tenant_id,
            deployment_ref,
        )
        if registration is None:
            return None
        return registration.tools


class DeploymentRecordPolicyResolver:
    """Answer from the deployment record the server holds.

    The deployment is fetched through an injected coroutine rather than a
    :class:`~zeroth.service.deployments.DeploymentService`, because governance
    may not depend on the service layer.

    **A deployment that cannot be loaded raises rather than yielding no
    bindings.** An earlier revision returned ``()`` there, on the reasoning
    that the client's bindings are only ever added to this set. That reasoning
    is wrong in the case that matters: when the client also sends none, the
    evaluator is handed the empty set, and evaluating zero policies *admits*.
    ``ToolDecisionService`` turns the raise into a recorded
    ``policy_unavailable`` denial, so an unloadable deployment refuses instead.
    """

    def __init__(self, get_deployment: Callable[[str], Awaitable[Any]]) -> None:
        self._get_deployment = get_deployment

    async def policy_bindings_for(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> tuple[str, ...]:
        """Return the deployment's own ``policy_bindings``.

        Raises:
            LookupError: If no deployment answers to *deployment_ref*. The
                bindings are unknown, not empty.
        """
        del tenant_id
        deployment = await self._get_deployment(deployment_ref)
        if deployment is None:
            raise LookupError("deployment carries no resolvable policy bindings")
        return tuple(getattr(deployment, "policy_bindings", ()) or ())


class PolicyApprovalGate:
    """Hold a side-effecting call when a resolved policy demands a human.

    ``PolicyDefinition.approval_required_for_side_effects`` is honoured at the
    node gate (``runtime/orchestration/policy_gate.py:183``) and by the agent
    runner (``runtime/agents/runner.py:565``), but a *tool decision* consulted
    nothing: production built the decision service with no gate at all, so the
    default :class:`~zeroth.governance.decisions.service.NoApprovalRequired`
    answered "no hold" for every call, including one whose policy said
    otherwise. Audit round 1 reproduced exactly that.

    The flag is read from the same registry
    :meth:`~zeroth.governance.policy.guard.PolicyGuard.evaluate_run_admission`
    resolves against, so the two cannot disagree about which policies apply.
    It is *not* read from ``RunAdmissionResult``: that model is pinned by the
    immutable legacy-surface fixture
    (``tests/contracts/fixtures/backend_surface_legacy.json``), which forbids
    even an additive field, so the flag could not be carried through the
    admission result without breaking a frozen contract.

    **The reference is opaque, not a resolvable approval record.**
    ``ApprovalService.create_pending`` needs a ``Run`` and a
    ``HumanApprovalNode``; a standalone tool call has neither, and inventing a
    run-scoped approval surface is outside this change. What the audited defect
    turns on is that such a call is *not allowed*, and it no longer is.
    """

    def __init__(self, policy_registry: Any) -> None:
        self._policy_registry = policy_registry

    async def requires_approval(
        self,
        request: DecisionRequest,
        policy_bindings: tuple[str, ...],
    ) -> str | None:
        """Return an approval reference when policy demands one, else ``None``.

        Args:
            request: The decision under evaluation.
            policy_bindings: The effective, server-derived binding set.

        Returns:
            A deterministic reference derived from the tenant and idempotency
            key -- so a retry under the same key yields the same one -- or
            ``None`` when no resolved policy requires approval for side
            effects. A read-only call is never held: the flag is about side
            effects, and holding a read would be a rule nobody wrote.

        Raises:
            Exception: If the registry cannot be consulted. Deliberately not
                caught here -- ``_approval_verdict`` turns any raise from a
                gate into a denial, so an unreadable policy set refuses the
                call rather than quietly allowing it.
        """
        if request.action.requires_approval:
            return f"tool-approval:{request.tenant_id}:{request.idempotency_key}"
        if request.action.side_effect != "side_effecting":
            return None
        for ref in sorted(set(policy_bindings)):
            try:
                policy = self._policy_registry.resolve(ref)
            except KeyError:
                # An unresolvable binding is already a denial inside
                # ``evaluate_run_admission``; this gate runs only on calls it
                # admitted, so the binding set here resolved cleanly there.
                continue
            if getattr(policy, "approval_required_for_side_effects", False):
                return f"tool-approval:{request.tenant_id}:{request.idempotency_key}"
        return None


__all__ = [
    "DeploymentPolicyResolver",
    "DeploymentRecordPolicyResolver",
    "PolicyApprovalGate",
    "RegisteredInventoryLookup",
    "ToolInventoryLookup",
]

"""Policy evaluation helpers.

This module contains the main PolicyGuard class that decides whether a node
is allowed to run, based on the policies attached to the graph and the node.
It also has a helper for filtering environment variables (secrets) according
to policy rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.graph import Graph, Node
from zeroth.policy.models import Capability, EnforcementResult, PolicyDecision, PolicyDefinition
from zeroth.policy.registry import CapabilityRegistry, PolicyRegistry
from zeroth.runs import Run


class PolicyGuard:
    """Checks whether a node is allowed to execute, given the active policies.

    Before a node runs, the guard collects all policies from the graph and the
    node, then compares the node's required capabilities against what those
    policies allow or deny.  The result is an EnforcementResult that says
    "allow" or "deny" (with a reason).
    """

    def __init__(
        self,
        *,
        policy_registry: PolicyRegistry | None = None,
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        self.policy_registry = policy_registry or PolicyRegistry()
        self.capability_registry = capability_registry or CapabilityRegistry()

    def evaluate(
        self,
        graph: Graph,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> EnforcementResult:
        """Decide whether a node is allowed to run under the current policies.

        Gathers policies from both the graph and the node, resolves the node's
        required capabilities, and returns an EnforcementResult indicating
        ALLOW or DENY along with the effective capabilities and constraints.
        """
        # These params are part of the interface but unused here; delete to avoid lint warnings
        del run, input_payload
        policies = [self.policy_registry.resolve(ref) for ref in graph.policy_bindings]
        policies.extend(self.policy_registry.resolve(ref) for ref in node.policy_bindings)

        # Required capabilities come from the node; policies only decide whether they are permitted.
        required_capabilities = {
            self.capability_registry.resolve(ref) for ref in node.capability_bindings
        }
        allowed = self._allowed_capabilities(policies)
        denied = {capability for policy in policies for capability in policy.denied_capabilities}

        rejected = [
            capability for capability in sorted(required_capabilities) if capability in denied
        ]
        if rejected:
            rejected_labels = ", ".join(capability.value for capability in rejected)
            return EnforcementResult(
                decision=PolicyDecision.DENY,
                reason=f"capability denied: {rejected_labels}",
                effective_capabilities=required_capabilities - set(rejected),
                allowed_secrets=self._allowed_secrets(policies),
                network_mode=self._network_mode(policies),
                timeout_override_seconds=self._timeout_override(policies),
                sandbox_strictness_mode=self._strictness_mode(policies),
            )

        if allowed is not None:
            missing = [
                capability
                for capability in sorted(required_capabilities)
                if capability not in allowed
            ]
            if missing:
                missing_labels = ", ".join(capability.value for capability in missing)
                return EnforcementResult(
                    decision=PolicyDecision.DENY,
                    reason=f"capability not allowed: {missing_labels}",
                    effective_capabilities=required_capabilities - set(missing),
                    allowed_secrets=self._allowed_secrets(policies),
                    network_mode=self._network_mode(policies),
                    timeout_override_seconds=self._timeout_override(policies),
                    sandbox_strictness_mode=self._strictness_mode(policies),
                )

        return EnforcementResult(
            decision=PolicyDecision.ALLOW,
            effective_capabilities=required_capabilities,
            allowed_secrets=self._allowed_secrets(policies),
            network_mode=self._network_mode(policies),
            timeout_override_seconds=self._timeout_override(policies),
            sandbox_strictness_mode=self._strictness_mode(policies),
        )

    def _allowed_capabilities(
        self,
        policies: list[PolicyDefinition],
    ) -> set[Capability] | None:
        """Return the intersection of all policies' allowed-capability lists.

        If no policy specifies an allow-list, returns None (meaning
        "no restriction").  When multiple policies each have an allow-list,
        only capabilities present in *every* list are kept.
        """
        allowed_lists = [
            set(policy.allowed_capabilities) for policy in policies if policy.allowed_capabilities
        ]
        if not allowed_lists:
            return None
        # Multiple allow-lists get stricter together: a capability must survive every policy.
        allowed = allowed_lists[0]
        for policy_allowed in allowed_lists[1:]:
            allowed &= policy_allowed
        return allowed

    def _allowed_secrets(self, policies: list[PolicyDefinition]) -> list[str]:
        """Return the intersection of all policies' allowed-secret lists.

        Only secrets that appear in every policy's allow-list survive.
        If no policy specifies a list, returns an empty list.
        """
        allowlists = [policy.allowed_secrets for policy in policies if policy.allowed_secrets]
        if not allowlists:
            return []
        current = set(allowlists[0])
        for policy_allowed in allowlists[1:]:
            current &= set(policy_allowed)
        return sorted(current)

    def _network_mode(self, policies: list[PolicyDefinition]) -> str | None:
        """Return the last policy's network mode setting, or None if unset."""
        modes = [policy.network_mode for policy in policies if policy.network_mode is not None]
        return modes[-1] if modes else None

    def _timeout_override(self, policies: list[PolicyDefinition]) -> float | None:
        """Return the last policy's timeout override, or None if unset."""
        overrides = [
            policy.timeout_override_seconds
            for policy in policies
            if policy.timeout_override_seconds is not None
        ]
        return overrides[-1] if overrides else None

    def _strictness_mode(self, policies: list[PolicyDefinition]) -> str | None:
        """Return the last policy's sandbox strictness mode, or None if unset."""
        modes = [
            policy.sandbox_strictness_mode
            for policy in policies
            if policy.sandbox_strictness_mode is not None
        ]
        return modes[-1] if modes else None


def apply_secret_policy(
    env: Mapping[str, str],
    *,
    allowed_secrets: list[str],
    secret_access_enabled: bool,
) -> dict[str, str]:
    """Filter environment variables so only policy-approved secrets get through.

    Returns a dictionary containing only the env vars whose names appear in
    ``allowed_secrets``.  If secret access is disabled or no secrets are
    allowed, returns an empty dictionary.
    """

    if not secret_access_enabled:
        return {}
    if not allowed_secrets:
        return {}
    # Secret filtering is name-based here; the caller decides which names count as approved.
    return {key: value for key, value in env.items() if key in set(allowed_secrets)}

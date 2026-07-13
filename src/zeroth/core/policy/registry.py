"""In-memory policy and capability registries.

Registries act as lookup tables: you register policies and capabilities
under string names (refs), and later resolve those names back to the
real objects.  This keeps graph and node definitions lightweight because
they only store short reference strings.
"""

from __future__ import annotations

from zeroth.core.policy.models import Capability, PolicyDefinition


class CapabilityRegistry:
    """A lookup table that maps short string refs to Capability values.

    Use this to register capabilities by name so that graphs and nodes
    can reference them without importing the Capability enum directly.
    """

    def __init__(self) -> None:
        self._refs: dict[str, Capability] = {}

    def register(self, ref: str, capability: Capability) -> Capability:
        """Store a capability under the given ref name and return it."""
        self._refs[ref] = capability
        return capability

    def resolve(self, ref: str) -> Capability:
        """Look up a capability by its ref name. Raises KeyError if not found."""
        try:
            return self._refs[ref]
        except KeyError as exc:
            raise KeyError(ref) from exc


class PolicyRegistry:
    """A lookup table that maps policy IDs to full PolicyDefinition objects.

    Register policies here so the PolicyGuard can find them when it
    evaluates a graph or node.
    """

    def __init__(self) -> None:
        self._policies: dict[str, PolicyDefinition] = {}

    def register(self, policy: PolicyDefinition) -> PolicyDefinition:
        """Store a policy definition, keyed by its policy_id, and return it."""
        self._policies[policy.policy_id] = policy
        return policy

    def resolve(self, ref: str) -> PolicyDefinition:
        """Look up a policy by its ID. Raises KeyError if not found."""
        try:
            return self._policies[ref]
        except KeyError as exc:
            raise KeyError(ref) from exc


def default_capability_registry() -> CapabilityRegistry:
    """Build a CapabilityRegistry that resolves every capability by its value.

    Each :class:`Capability` is registered under its own string value (e.g.
    ``"memory_read"`` -> ``Capability.MEMORY_READ``). This is the ref scheme the
    served path expects: graphs declare ``capability_bindings=["memory_read",
    ...]`` and the wired-in :class:`PolicyGuard` resolves them without any
    app-specific registration. Apps that use a bespoke ref scheme (e.g.
    ``"capability://memory-read"``) build and inject their own registry instead.
    """
    registry = CapabilityRegistry()
    for capability in Capability:
        registry.register(capability.value, capability)
    return registry

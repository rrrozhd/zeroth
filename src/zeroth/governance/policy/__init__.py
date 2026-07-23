"""Policy and capability enforcement primitives.

This package lets you define what agents are and aren't allowed to do.
It provides models for capabilities (like network access or file writes),
policy definitions that allow or deny those capabilities, and a guard that
checks policies before a node runs. The ``Capability`` enum itself is
authored graph vocabulary defined in :mod:`zeroth.contracts.graph.models`
and republished here.
"""

from zeroth.governance.policy.errors import (
    CapabilityDeniedError,
    parse_effective_capabilities,
    require_capabilities,
)
from zeroth.governance.policy.guard import PolicyGuard, RunAdmissionRequest, apply_secret_policy
from zeroth.governance.policy.models import (
    Capability,
    EnforcementResult,
    PolicyDecision,
    PolicyDefinition,
    RunAdmissionResult,
)
from zeroth.governance.policy.registry import (
    CapabilityRegistry,
    PolicyRegistry,
    default_capability_registry,
)

__all__ = [
    "Capability",
    "CapabilityDeniedError",
    "CapabilityRegistry",
    "EnforcementResult",
    "PolicyDecision",
    "PolicyDefinition",
    "PolicyGuard",
    "PolicyRegistry",
    "RunAdmissionRequest",
    "RunAdmissionResult",
    "apply_secret_policy",
    "default_capability_registry",
    "parse_effective_capabilities",
    "require_capabilities",
]

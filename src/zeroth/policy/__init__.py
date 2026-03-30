"""Policy and capability enforcement primitives.

This package lets you define what agents are and aren't allowed to do.
It provides models for capabilities (like network access or file writes),
policy definitions that allow or deny those capabilities, and a guard that
checks policies before a node runs.
"""

from zeroth.policy.guard import PolicyGuard, apply_secret_policy
from zeroth.policy.models import Capability, EnforcementResult, PolicyDecision, PolicyDefinition
from zeroth.policy.registry import CapabilityRegistry, PolicyRegistry

__all__ = [
    "Capability",
    "CapabilityRegistry",
    "EnforcementResult",
    "PolicyDecision",
    "PolicyDefinition",
    "PolicyGuard",
    "PolicyRegistry",
    "apply_secret_policy",
]

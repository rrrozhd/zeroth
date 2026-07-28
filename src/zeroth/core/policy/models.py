"""Legacy import path for :mod:`zeroth.governance.policy.models`.

``Capability`` is defined in :mod:`zeroth.contracts.graph.models` and
republished through the policy surface.
"""

from zeroth.governance.policy.models import (
    Capability,
    EnforcementResult,
    PolicyDecision,
    PolicyDefinition,
    RunAdmissionResult,
)

__all__ = [
    "Capability",
    "EnforcementResult",
    "PolicyDecision",
    "PolicyDefinition",
    "RunAdmissionResult",
]

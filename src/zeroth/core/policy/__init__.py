"""Legacy import path for the governance policy package.

The policy subsystem lives in :mod:`zeroth.governance.policy`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.governance.policy import (
    Capability,
    CapabilityDeniedError,
    CapabilityRegistry,
    EnforcementResult,
    PolicyDecision,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
    apply_secret_policy,
    default_capability_registry,
    parse_effective_capabilities,
    require_capabilities,
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
    "apply_secret_policy",
    "default_capability_registry",
    "parse_effective_capabilities",
    "require_capabilities",
]

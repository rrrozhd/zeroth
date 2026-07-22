"""Legacy import path for :mod:`zeroth.governance.policy.errors`."""

from zeroth.governance.policy.errors import (
    CapabilityDeniedError,
    parse_effective_capabilities,
    require_capabilities,
)

__all__ = [
    "CapabilityDeniedError",
    "parse_effective_capabilities",
    "require_capabilities",
]

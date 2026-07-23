"""Legacy import path for :mod:`zeroth.governance.policy.guard`."""

from zeroth.governance.policy.guard import (
    PolicyGuard,
    RunAdmissionRequest,
    apply_secret_policy,
)

__all__ = ["PolicyGuard", "RunAdmissionRequest", "apply_secret_policy"]

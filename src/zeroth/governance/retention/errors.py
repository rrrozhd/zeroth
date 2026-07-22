"""Public exceptions raised by the retention erasure surface.

The definitions live here because the collaborators that raise them may not
import the service facade -- the same import constraint that moved the
orchestration exceptions in Task 8. ``zeroth.governance.retention.erasure_service``
re-exports both names, so every documented import path is unchanged, and the
protected surface keeps recording that module as their location.
"""

from __future__ import annotations


class StaleCleanupClaimError(RuntimeError):
    """Raised when an expired cleanup worker attempts to mutate newer state."""


class LegalHoldError(RuntimeError):
    """Raised when erasure is refused because an active legal hold covers it."""

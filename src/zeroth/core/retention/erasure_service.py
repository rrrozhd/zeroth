"""Legacy import path for :mod:`zeroth.governance.retention.erasure_service`."""

from zeroth.governance.retention.erasure_service import (
    LegalHoldError,
    RetentionErasureService,
    StaleCleanupClaimError,
)

__all__ = ["LegalHoldError", "RetentionErasureService", "StaleCleanupClaimError"]

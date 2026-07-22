"""Legacy import path for :mod:`zeroth.governance.audit.verifier`.

The private digest and PII-commitment helpers are republished because
cross-package consumers (the erasure tests and repository) imported them
from this path.
"""

from zeroth.governance.audit.verifier import (
    AuditContinuityVerifier,
    _compute_pii_commitments,
    _compute_record_digest,
    compute_chained_record,
)

__all__ = [
    "AuditContinuityVerifier",
    "_compute_pii_commitments",
    "_compute_record_digest",
    "compute_chained_record",
]

"""WS-E retention & right-to-erasure.

Per-tenant retention TTLs, legal holds, and a full-surface erasure service that
removes PII WITHOUT breaking the append-only audit hash-chain (commitment-digest
crypto-erasure — see docs/retention-and-erasure.md for the honest GDPR posture).
"""

from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.core.retention.econ_eraser import EconEventEraser, SqlAlchemyEconEventEraser
from zeroth.core.retention.erasure_service import (
    LegalHoldError,
    RetentionErasureService,
)
from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
from zeroth.core.retention.models import (
    ErasureResult,
    LegalHold,
    RetentionPolicy,
    TenantHolds,
)
from zeroth.core.retention.policy_repository import RetentionPolicyRepository
from zeroth.core.retention.worker import RetentionPurgeWorker

__all__ = [
    "EconEventEraser",
    "ErasureResult",
    "LegalHold",
    "LegalHoldError",
    "LegalHoldRepository",
    "RetentionAuditLogRepository",
    "RetentionErasureService",
    "RetentionPolicy",
    "RetentionPolicyRepository",
    "RetentionPurgeWorker",
    "SqlAlchemyEconEventEraser",
    "TenantHolds",
]

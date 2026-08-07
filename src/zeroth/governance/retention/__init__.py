"""Data retention governance: retention TTLs, legal holds, and right-to-erasure.

Per-tenant retention TTLs, legal holds, and a full-surface erasure service that
removes PII WITHOUT breaking the append-only audit hash-chain (commitment-digest
crypto-erasure — see docs/retention-and-erasure.md for the honest GDPR posture).

The erasure service is a facade over five collaborators, each owning one
concern:

| Module | Owns |
| --- | --- |
| ``manifests`` | building the cleanup manifest and projecting it into results |
| ``replay`` | folding legacy retention audit entries back into claim state |
| ``claims`` | claim leases, fencing, and the CAS writes behind them |
| ``executor`` | running manifest operations against external surfaces |
| ``compatibility`` | the legacy per-step retention log entries |
| ``errors`` | the two public exception types |

Task 13 merged the remaining legacy modules — the erasure facade, worker,
coordination, econ eraser, models, cleanup manifest, and the four
repositories — into this package, so the whole retention surface now
resolves eagerly from one home. ``zeroth.governance.retention`` keeps
republishing the same objects, resolving the erasure service lazily for
the reason documented there;
``tests/governance/retention/test_cold_import.py`` pins both directions
from subprocesses.
"""

from __future__ import annotations

from zeroth.governance.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.governance.retention.claims import CleanupClaims
from zeroth.governance.retention.compatibility import CompatibilityLog, result_detail
from zeroth.governance.retention.econ_eraser import EconEventEraser, SqlAlchemyEconEventEraser
from zeroth.governance.retention.erasure_service import RetentionErasureService
from zeroth.governance.retention.errors import LegalHoldError, StaleCleanupClaimError
from zeroth.governance.retention.executor import CleanupExecutor
from zeroth.governance.retention.legal_hold_repository import LegalHoldRepository
from zeroth.governance.retention.manifests import (
    build_cleanup_manifest,
    manifest_complete,
    result_from_manifest,
)
from zeroth.governance.retention.models import (
    ErasureResult,
    LegalHold,
    RetentionPolicy,
    TenantHolds,
)
from zeroth.governance.retention.policy_repository import RetentionPolicyRepository
from zeroth.governance.retention.replay import CleanupReplayState, replay_cleanup_state
from zeroth.governance.retention.worker import RetentionPurgeWorker

__all__ = [
    "CleanupClaims",
    "CleanupExecutor",
    "CleanupReplayState",
    "CompatibilityLog",
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
    "StaleCleanupClaimError",
    "TenantHolds",
    "build_cleanup_manifest",
    "manifest_complete",
    "replay_cleanup_state",
    "result_detail",
    "result_from_manifest",
]

"""WS-E retention & right-to-erasure.

Per-tenant retention TTLs, legal holds, and a full-surface erasure service that
removes PII WITHOUT breaking the append-only audit hash-chain (commitment-digest
crypto-erasure — see docs/retention-and-erasure.md for the honest GDPR posture).

The erasure service is decomposed across :mod:`zeroth.governance.retention` and
is resolved from :mod:`zeroth.core.retention.erasure_service` on first access,
so ``from zeroth.core.retention import RetentionErasureService`` is unchanged.

That resolution stays lazy for the reason that made the decomposition possible.
Every extracted collaborator imports the manifest and state models, which still
live in this package -- so importing one executes this ``__init__``. Resolving
the service here eagerly would import the collaborators back while they are
still initializing, and the canonical package would stop being importable in a
cold interpreter. ``tests/governance/retention/test_cold_import.py`` pins both
directions from subprocesses, because the in-process suite always has
``zeroth.core`` warm and cannot see the cycle.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.core.retention.econ_eraser import EconEventEraser, SqlAlchemyEconEventEraser
from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
from zeroth.core.retention.models import (
    ErasureResult,
    LegalHold,
    RetentionPolicy,
    TenantHolds,
)
from zeroth.core.retention.policy_repository import RetentionPolicyRepository
from zeroth.core.retention.worker import RetentionPurgeWorker

if TYPE_CHECKING:
    from zeroth.core.retention.erasure_service import (
        LegalHoldError,
        RetentionErasureService,
    )

_EXPORTS = {
    "LegalHoldError": "zeroth.core.retention.erasure_service",
    "RetentionErasureService": "zeroth.core.retention.erasure_service",
}

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


def __getattr__(name: str) -> object:
    """Resolve the erasure service from its own module on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))

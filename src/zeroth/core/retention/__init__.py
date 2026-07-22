"""Legacy import path for the governance retention package.

The retention subsystem lives in :mod:`zeroth.governance.retention`; this
package republishes the same objects for compatibility. Import from the
canonical location instead (see docs/backend-import-migration.md).

The erasure service keeps resolving lazily, mirroring the layout this
package had while the decomposition was in flight; eager resolution here
would put the whole erasure surface on the import path of anything that
touches a retention model.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from zeroth.governance.retention import (
    EconEventEraser,
    ErasureResult,
    LegalHold,
    LegalHoldRepository,
    RetentionAuditLogRepository,
    RetentionPolicy,
    RetentionPolicyRepository,
    RetentionPurgeWorker,
    SqlAlchemyEconEventEraser,
    TenantHolds,
)

if TYPE_CHECKING:
    from zeroth.governance.retention.erasure_service import (
        LegalHoldError,
        RetentionErasureService,
    )

_EXPORTS = {
    "LegalHoldError": "zeroth.governance.retention.erasure_service",
    "RetentionErasureService": "zeroth.governance.retention.erasure_service",
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

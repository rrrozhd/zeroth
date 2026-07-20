"""Legacy import path for the governed approvals package.

The approvals subsystem lives in :mod:`zeroth.governance.approvals`; this
package republishes the same objects for compatibility. Import from the
canonical location instead (see docs/backend-import-migration.md).
"""

from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRepository,
    ApprovalResolution,
    ApprovalService,
    ApprovalStatus,
    HumanInteractionType,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "ApprovalRepository",
    "ApprovalResolution",
    "ApprovalService",
    "ApprovalStatus",
    "HumanInteractionType",
]

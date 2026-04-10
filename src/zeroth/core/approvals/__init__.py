"""Approvals package — provides everything you need to create, store, and resolve
human approval requests within agent workflows.

This package re-exports the key models, the database repository, and the
high-level service so callers can simply ``from zeroth.core.approvals import ...``.
"""

from zeroth.core.approvals.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalStatus,
    HumanInteractionType,
)
from zeroth.core.approvals.repository import ApprovalRepository
from zeroth.core.approvals.service import ApprovalService

__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "ApprovalRepository",
    "ApprovalResolution",
    "ApprovalService",
    "ApprovalStatus",
    "HumanInteractionType",
]

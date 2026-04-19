"""Audit package for tracking what happens during agent runs.

This package provides everything you need to record, store, query, and
review audit trails: data models, a SQLite-backed repository, payload
sanitization (to strip secrets), and a timeline assembler for viewing
events in order.
"""

from zeroth.core.audit.evidence import build_summary, collect_policy_events
from zeroth.core.audit.models import (
    ApprovalActionRecord,
    AuditContinuityReport,
    AuditQuery,
    AuditRedactionConfig,
    AuditTimeline,
    MemoryAccessRecord,
    NodeAuditRecord,
    ToolCallRecord,
)
from zeroth.core.audit.repository import AuditRepository
from zeroth.core.audit.sanitizer import PayloadSanitizer
from zeroth.core.audit.timeline import AuditTimelineAssembler
from zeroth.core.audit.verifier import AuditContinuityVerifier, compute_chained_record

__all__ = [
    "ApprovalActionRecord",
    "AuditContinuityReport",
    "AuditContinuityVerifier",
    "AuditQuery",
    "AuditRedactionConfig",
    "AuditRepository",
    "AuditTimeline",
    "AuditTimelineAssembler",
    "MemoryAccessRecord",
    "NodeAuditRecord",
    "PayloadSanitizer",
    "ToolCallRecord",
    "build_summary",
    "collect_policy_events",
    "compute_chained_record",
]

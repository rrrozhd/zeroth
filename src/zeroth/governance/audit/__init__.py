"""Audit evidence and emission domain.

This package provides everything you need to record, store, query, and
review audit trails: data models, a SQLite-backed repository, payload
sanitization (to strip secrets), a timeline assembler for viewing events
in order, and the governed audit emitters consolidated from the vendored
governai bundle (see docs/governed-capability-disposition.md).
"""

from zeroth.governance.audit.emitter import AuditEmitter, emit_event
from zeroth.governance.audit.evidence import build_summary, collect_policy_events
from zeroth.governance.audit.models import (
    ApprovalActionRecord,
    AuditContinuityReport,
    AuditQuery,
    AuditRedactionConfig,
    AuditTimeline,
    MemoryAccessRecord,
    NodeAuditRecord,
    ToolCallRecord,
)
from zeroth.governance.audit.redis import RedisAuditEmitter
from zeroth.governance.audit.repository import AuditRepository
from zeroth.governance.audit.sanitizer import PayloadSanitizer
from zeroth.governance.audit.timeline import AuditTimelineAssembler
from zeroth.governance.audit.verifier import AuditContinuityVerifier, compute_chained_record

__all__ = [
    "ApprovalActionRecord",
    "AuditContinuityReport",
    "AuditContinuityVerifier",
    "AuditEmitter",
    "AuditQuery",
    "AuditRedactionConfig",
    "AuditRepository",
    "AuditTimeline",
    "AuditTimelineAssembler",
    "MemoryAccessRecord",
    "NodeAuditRecord",
    "PayloadSanitizer",
    "RedisAuditEmitter",
    "ToolCallRecord",
    "build_summary",
    "collect_policy_events",
    "compute_chained_record",
    "emit_event",
]

"""Canonical import surface for the governance audit package.

Non-golden boundary tests for the Task 13 audit consolidation: the canonical
``zeroth.governance.audit`` package must publish the same objects the legacy
``zeroth.core.audit`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.

The consolidation also folds the vendored governed audit emitters into the
same canonical package: ``AuditEmitter``, ``emit_event``, and
``RedisAuditEmitter`` must be identical through ``zeroth.governance.audit``
and the legacy ``zeroth.core.governed.audit`` modules, per their disposition
rows in docs/governed-capability-disposition.md.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_audit_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import audit as legacy
    from zeroth.governance import audit as canonical

    assert canonical.ApprovalActionRecord is legacy.ApprovalActionRecord
    assert canonical.AuditContinuityReport is legacy.AuditContinuityReport
    assert canonical.AuditContinuityVerifier is legacy.AuditContinuityVerifier
    assert canonical.AuditQuery is legacy.AuditQuery
    assert canonical.AuditRedactionConfig is legacy.AuditRedactionConfig
    assert canonical.AuditRepository is legacy.AuditRepository
    assert canonical.AuditTimeline is legacy.AuditTimeline
    assert canonical.AuditTimelineAssembler is legacy.AuditTimelineAssembler
    assert canonical.MemoryAccessRecord is legacy.MemoryAccessRecord
    assert canonical.NodeAuditRecord is legacy.NodeAuditRecord
    assert canonical.PayloadSanitizer is legacy.PayloadSanitizer
    assert canonical.ToolCallRecord is legacy.ToolCallRecord
    assert canonical.build_summary is legacy.build_summary
    assert canonical.collect_policy_events is legacy.collect_policy_events
    assert canonical.compute_chained_record is legacy.compute_chained_record


def test_audit_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.audit import coordination as legacy_coordination
    from zeroth.core.audit import erasure_schema as legacy_erasure_schema
    from zeroth.core.audit import models as legacy_models
    from zeroth.core.audit import verifier as legacy_verifier
    from zeroth.governance.audit import coordination as canonical_coordination
    from zeroth.governance.audit import erasure_schema as canonical_erasure_schema
    from zeroth.governance.audit import models as canonical_models
    from zeroth.governance.audit import verifier as canonical_verifier

    assert canonical_models.NodeAuditRecord is legacy_models.NodeAuditRecord
    assert canonical_models.TokenUsage is legacy_models.TokenUsage
    assert (
        canonical_coordination.AuditChainOrderingError
        is legacy_coordination.AuditChainOrderingError
    )
    assert canonical_coordination.order_audit_records is legacy_coordination.order_audit_records
    assert (
        canonical_erasure_schema.AUDIT_CLEANUP_PAYLOAD_FIELDS
        is legacy_erasure_schema.AUDIT_CLEANUP_PAYLOAD_FIELDS
    )
    assert canonical_verifier.compute_chained_record is legacy_verifier.compute_chained_record
    assert canonical_verifier._compute_record_digest is legacy_verifier._compute_record_digest
    assert (
        canonical_verifier._compute_pii_commitments is legacy_verifier._compute_pii_commitments
    )


def test_governed_audit_emitters_are_consolidated_into_governance_audit() -> None:
    from zeroth.core.governed.audit import emitter as legacy_emitter
    from zeroth.core.governed.audit import redis as legacy_redis
    from zeroth.governance import audit as canonical
    from zeroth.governance.audit import emitter as canonical_emitter
    from zeroth.governance.audit import redis as canonical_redis

    assert canonical_emitter.AuditEmitter is legacy_emitter.AuditEmitter
    assert canonical_emitter.emit_event is legacy_emitter.emit_event
    assert canonical_redis.RedisAuditEmitter is legacy_redis.RedisAuditEmitter
    assert canonical.AuditEmitter is legacy_emitter.AuditEmitter
    assert canonical.emit_event is legacy_emitter.emit_event
    assert canonical.RedisAuditEmitter is legacy_redis.RedisAuditEmitter


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.audit", "zeroth.core.audit"),
        ("zeroth.core.audit", "zeroth.governance.audit"),
        ("zeroth.governance.audit", "zeroth.core.governed.audit.redis"),
        ("zeroth.core.governed.audit.redis", "zeroth.governance.audit"),
    ],
)
def test_audit_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"

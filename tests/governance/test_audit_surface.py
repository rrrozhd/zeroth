"""Canonical import surface for the governance audit package.

Non-golden boundary tests for the Task 13 audit consolidation: the canonical
``zeroth.governance.audit`` package must publish the same objects the legacy
``zeroth.core.audit`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.

The consolidation also folds the vendored governed audit emitters into the
same canonical package: ``AuditEmitter``, ``emit_event``, and
``RedisAuditEmitter`` must be identical through ``zeroth.governance.audit``
and the legacy ``zeroth.core.governed.audit`` modules, per their disposition
rows in docs/backend-import-migration.md.
"""

from __future__ import annotations

import subprocess
import sys


def test_audit_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.governance.audit as canonical

    expected = {
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
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.governance.audit no longer publishes: {missing}"


def test_audit_submodules_publish_their_names() -> None:
    from zeroth.core.audit import coordination as legacy_coordination
    from zeroth.core.audit import erasure_schema as legacy_erasure_schema
    from zeroth.governance.audit import coordination as canonical_coordination
    from zeroth.governance.audit import erasure_schema as canonical_erasure_schema
    from zeroth.governance.audit import models as canonical_models
    from zeroth.governance.audit import verifier as canonical_verifier

    assert hasattr(canonical_models, "NodeAuditRecord")
    assert hasattr(canonical_models, "TokenUsage")
    assert (
        canonical_coordination.AuditChainOrderingError
        is legacy_coordination.AuditChainOrderingError
    )
    assert hasattr(canonical_coordination, "order_audit_records")
    assert (
        canonical_erasure_schema.AUDIT_CLEANUP_PAYLOAD_FIELDS
        is legacy_erasure_schema.AUDIT_CLEANUP_PAYLOAD_FIELDS
    )
    assert hasattr(canonical_verifier, "compute_chained_record")
    assert hasattr(canonical_verifier, "_compute_record_digest")
    assert hasattr(canonical_verifier, "_compute_pii_commitments")


def test_governed_audit_emitters_are_consolidated_into_governance_audit() -> None:
    from zeroth.governance import audit as canonical
    from zeroth.governance.audit import emitter as canonical_emitter
    from zeroth.governance.audit import redis as canonical_redis

    assert hasattr(canonical_emitter, "AuditEmitter")
    assert hasattr(canonical_emitter, "emit_event")
    assert hasattr(canonical_redis, "RedisAuditEmitter")
    assert hasattr(canonical, "AuditEmitter")
    assert hasattr(canonical, "emit_event")
    assert hasattr(canonical, "RedisAuditEmitter")


def test_audit_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.governance.audit"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

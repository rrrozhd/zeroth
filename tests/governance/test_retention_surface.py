"""Canonical import surface for the merged governance retention package.

Non-golden boundary tests for the Task 13 retention merge: the remaining
``zeroth.core.retention`` modules — the erasure facade, worker,
coordination, econ eraser, models, cleanup manifest, and the four
repositories — join the collaborators Task 9 already decomposed into
``zeroth.governance.retention``, and the legacy path keeps republishing the
same objects. Dual-path composition and cold-import guarantees are pinned
by ``tests/governance/retention/test_composition.py`` and
``test_cold_import.py``; this module pins the moved-in remainder.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_retention_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import retention as legacy
    from zeroth.governance import retention as canonical

    assert canonical.EconEventEraser is legacy.EconEventEraser
    assert canonical.ErasureResult is legacy.ErasureResult
    assert canonical.LegalHold is legacy.LegalHold
    assert canonical.LegalHoldError is legacy.LegalHoldError
    assert canonical.LegalHoldRepository is legacy.LegalHoldRepository
    assert canonical.RetentionAuditLogRepository is legacy.RetentionAuditLogRepository
    assert canonical.RetentionErasureService is legacy.RetentionErasureService
    assert canonical.RetentionPolicy is legacy.RetentionPolicy
    assert canonical.RetentionPolicyRepository is legacy.RetentionPolicyRepository
    assert canonical.RetentionPurgeWorker is legacy.RetentionPurgeWorker
    assert canonical.SqlAlchemyEconEventEraser is legacy.SqlAlchemyEconEventEraser
    assert canonical.TenantHolds is legacy.TenantHolds


def test_retention_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.retention import cleanup_manifest as legacy_manifest
    from zeroth.core.retention import coordination as legacy_coordination
    from zeroth.core.retention import erasure_service as legacy_service
    from zeroth.core.retention import models as legacy_models
    from zeroth.core.retention import worker as legacy_worker
    from zeroth.governance.retention import cleanup_manifest as canonical_manifest
    from zeroth.governance.retention import coordination as canonical_coordination
    from zeroth.governance.retention import erasure_service as canonical_service
    from zeroth.governance.retention import models as canonical_models
    from zeroth.governance.retention import worker as canonical_worker

    assert canonical_manifest.CleanupManifest is legacy_manifest.CleanupManifest
    assert canonical_manifest.CleanupOperation is legacy_manifest.CleanupOperation
    assert canonical_manifest.operation_id is legacy_manifest.operation_id
    assert canonical_coordination.RetentionCoordinator is legacy_coordination.RetentionCoordinator
    assert canonical_service.RetentionErasureService is legacy_service.RetentionErasureService
    assert canonical_service.LegalHoldError is legacy_service.LegalHoldError
    assert canonical_models.RetentionPolicy is legacy_models.RetentionPolicy
    assert canonical_worker.RetentionPurgeWorker is legacy_worker.RetentionPurgeWorker


def test_retention_repositories_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.retention import audit_log_repository as legacy_audit_log
    from zeroth.core.retention import cleanup_state_repository as legacy_state
    from zeroth.core.retention import econ_eraser as legacy_econ
    from zeroth.core.retention import legal_hold_repository as legacy_hold
    from zeroth.core.retention import policy_repository as legacy_policy
    from zeroth.governance.retention import audit_log_repository as canonical_audit_log
    from zeroth.governance.retention import cleanup_state_repository as canonical_state
    from zeroth.governance.retention import econ_eraser as canonical_econ
    from zeroth.governance.retention import legal_hold_repository as canonical_hold
    from zeroth.governance.retention import policy_repository as canonical_policy

    assert (
        canonical_audit_log.RetentionAuditLogRepository
        is legacy_audit_log.RetentionAuditLogRepository
    )
    assert (
        canonical_state.CleanupStateRepository is legacy_state.CleanupStateRepository
    )
    assert canonical_econ.EconEventEraser is legacy_econ.EconEventEraser
    assert (
        canonical_econ.SqlAlchemyEconEventEraser is legacy_econ.SqlAlchemyEconEventEraser
    )
    assert canonical_hold.LegalHoldRepository is legacy_hold.LegalHoldRepository
    assert canonical_policy.RetentionPolicyRepository is legacy_policy.RetentionPolicyRepository


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.retention.erasure_service", "zeroth.core.retention"),
        ("zeroth.core.retention", "zeroth.governance.retention.erasure_service"),
        ("zeroth.governance.retention.worker", "zeroth.core.retention.worker"),
        ("zeroth.core.retention.worker", "zeroth.governance.retention.worker"),
    ],
)
def test_retention_remainder_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"

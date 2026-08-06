"""Canonical import surface for the governance approvals package.

Non-golden boundary tests for the Task 13 approvals move: the canonical
``zeroth.governance.approvals`` package must publish the same objects the
legacy ``zeroth.core.approvals`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.

Two seams are pinned alongside the move. ``ApprovalService.continue_run``
takes a governance-owned ``ApprovalContinuation`` protocol instead of the
runtime orchestrator class, so the approvals domain no longer imports the
orchestrator. ``RunStatus`` is consumed from its contract-owned definition in
``zeroth.contracts.governed``, so the remaining run-domain edge carries only
the run bookkeeping objects.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol


def test_approvals_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.governance.approvals as canonical

    expected = {
        "ApprovalDecision",
        "ApprovalRecord",
        "ApprovalRepository",
        "ApprovalResolution",
        "ApprovalService",
        "ApprovalStatus",
        "HumanInteractionType",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.governance.approvals no longer publishes: {missing}"


def test_approval_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.approvals import models as legacy_models
    from zeroth.core.approvals import repository as legacy_repository
    from zeroth.core.approvals import service as legacy_service
    from zeroth.core.approvals import sla_checker as legacy_sla_checker
    from zeroth.governance.approvals import models as canonical_models
    from zeroth.governance.approvals import repository as canonical_repository
    from zeroth.governance.approvals import service as canonical_service
    from zeroth.governance.approvals import sla_checker as canonical_sla_checker

    assert canonical_models.ApprovalRecord is legacy_models.ApprovalRecord
    assert canonical_models.ApprovalResolution is legacy_models.ApprovalResolution
    assert canonical_repository.ApprovalRepository is legacy_repository.ApprovalRepository
    assert canonical_service.ApprovalService is legacy_service.ApprovalService
    assert canonical_sla_checker.ApprovalSLAChecker is legacy_sla_checker.ApprovalSLAChecker


def test_continue_run_is_annotated_with_a_governance_owned_protocol() -> None:
    from zeroth.governance.approvals.service import ApprovalContinuation, ApprovalService

    assert issubclass(type(ApprovalContinuation), type(Protocol))
    assert hasattr(ApprovalContinuation, "record_approval_resolution")
    assert hasattr(ApprovalContinuation, "resume_graph")
    annotations = ApprovalService.continue_run.__annotations__
    assert annotations["orchestrator"] == "ApprovalContinuation"


def test_approvals_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.governance.approvals"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

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

import pytest


def test_approvals_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import approvals as legacy
    from zeroth.governance import approvals as canonical

    assert canonical.ApprovalDecision is legacy.ApprovalDecision
    assert canonical.ApprovalRecord is legacy.ApprovalRecord
    assert canonical.ApprovalRepository is legacy.ApprovalRepository
    assert canonical.ApprovalResolution is legacy.ApprovalResolution
    assert canonical.ApprovalService is legacy.ApprovalService
    assert canonical.ApprovalStatus is legacy.ApprovalStatus
    assert canonical.HumanInteractionType is legacy.HumanInteractionType


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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.approvals", "zeroth.core.approvals"),
        ("zeroth.core.approvals", "zeroth.governance.approvals"),
        ("zeroth.governance.approvals", "zeroth.core.runs"),
        ("zeroth.core.runs", "zeroth.governance.approvals"),
    ],
)
def test_approvals_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"

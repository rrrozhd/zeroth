from __future__ import annotations

from pathlib import Path

import pytest

from release.live_evaluation.handoff_checkpoint import (
    REQUIRED_EVIDENCE_ROOTS,
    ROOT,
    _validate_document,
)


@pytest.mark.parametrize(
    ("criterion_id", "filename"),
    (
        ("handoff.discrepancy-register", "discrepancy-register.md"),
        (
            "handoff.execution-and-rollback-instructions",
            "execution-and-rollback.md",
        ),
    ),
)
def test_reviewed_handoff_documents_satisfy_the_operator_contract(
    criterion_id: str,
    filename: str,
) -> None:
    path = Path("release/live_evaluation/handoff") / filename
    assert _validate_document(criterion_id, path).startswith("# Live evaluation")


def test_handoff_document_rejects_incomplete_content(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.md"
    path.write_text("# Discrepancy register\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="lacks required content"):
        _validate_document("handoff.discrepancy-register", path)


def test_handoff_links_the_current_native_safari_and_gap_audit_roots() -> None:
    assert ROOT.name == "handoff-checkpoint-20260825-4"
    assert "native-safari-loop-refresh-checkpoint-20260825-1" in REQUIRED_EVIDENCE_ROOTS
    assert "native-safari-retention-validation-checkpoint-20260825-1" in REQUIRED_EVIDENCE_ROOTS
    assert "retention-compliance-live-checkpoint-20260825-1" in REQUIRED_EVIDENCE_ROOTS
    assert "product-surface-inventory-checkpoint-20260825-1" in REQUIRED_EVIDENCE_ROOTS
    assert "acceptance-gap-audit-20260825-27" in REQUIRED_EVIDENCE_ROOTS
    assert "acceptance-gap-audit-20260825-26" not in REQUIRED_EVIDENCE_ROOTS
    assert "acceptance-gap-audit-20260824-13" not in REQUIRED_EVIDENCE_ROOTS

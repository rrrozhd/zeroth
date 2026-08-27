from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.economics_live_presentation_checkpoint import (
    EXPECTED_CRITERIA,
    build_checkpoint,
)
from release.live_evaluation.evidence import EvidenceStore


def _git_repository(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "fixture.txt").write_text("fixture\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)


def _source(root: Path, *, failure_mode: str = "fail_closed") -> Path:
    indexed = root / "indexed"
    report = root / "html-report"
    indexed.mkdir(parents=True)
    report.mkdir()
    json_name = "c3566137365031fc-economics-ledger-reconciliation.json"
    screenshot_name = "2725d467db422db5-economics-ledger-run-reconciliation.png"
    video_name = "170cdd45b567823d-video.webm"
    (indexed / json_name).write_text(json.dumps({
        "ledger_actual_usd": 0.0000134,
        "run_attributed_usd": 0,
        "difference_usd": 0.0000134,
        "active_exposure_usd": 0,
        "ambiguous_exposure_usd": 0,
        "synthetic_control_usd": 0.01,
        "failure_mode": failure_mode,
    }))
    (indexed / screenshot_name).write_bytes(b"\x89PNG\r\n\x1a\neconomics")
    (indexed / video_name).write_bytes(b"\x1aE\xdf\xa3video")
    (report / "index.html").write_text("<html><body>passed</body></html>")
    artifacts = [
        {"source": f"indexed/{json_name}", "destination": f"console/{json_name}"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
        {"source": f"indexed/{screenshot_name}", "destination": f"screenshots/{screenshot_name}"},
        {"source": f"indexed/{video_name}", "destination": f"videos/{video_name}"},
    ]
    (root / "results.json").write_text(json.dumps({
        "completed": True,
        "criteria": [
            {"criterion_id": criterion, "status": "pass"}
            for criterion in sorted(EXPECTED_CRITERIA)
        ],
        "artifacts": artifacts,
    }))
    return root


def test_build_checkpoint_seals_exact_economics_truth(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)
    destination = tmp_path / "sealed"

    build_checkpoint(
        source_root=_source(tmp_path / "source"),
        destination=destination,
        repository_root=repository,
    )

    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {row["criterion_id"] for row in acceptance["criteria"]} == EXPECTED_CRITERIA
    assert all(row["status"] == "pass" for row in acceptance["criteria"])


def test_build_checkpoint_refuses_fail_open_claim(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)

    with pytest.raises(RuntimeError, match="not the accepted live truth state"):
        build_checkpoint(
            source_root=_source(tmp_path / "source", failure_mode="fail_open"),
            destination=tmp_path / "sealed",
            repository_root=repository,
        )

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.audit_presentation_checkpoint import (
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


def _source(root: Path, *, signed: bool = True) -> Path:
    indexed = root / "indexed"
    report = root / "html-report"
    indexed.mkdir(parents=True)
    report.mkdir()
    verification_name = "a205fa8549064093-audit-chain-verification.json"
    verified_name = "545ca965962ab339-audit-chain-verified.png"
    configured_name = "aeb3b7d59dd6f1fe-audit-configured.png"
    video_name = "e35d374b035a1919-video.webm"
    (indexed / verification_name).write_text(json.dumps({
        "scope": "deployment:fixture",
        "verified": signed,
        "record_count": 3,
        "signature_verified": signed,
        "signing_key_id": "local-evaluation",
        "unsigned_record_count": 0,
    }))
    (indexed / verified_name).write_bytes(b"\x89PNG\r\n\x1a\nverified")
    (indexed / configured_name).write_bytes(b"\x89PNG\r\n\x1a\nconfigured")
    (indexed / video_name).write_bytes(b"\x1aE\xdf\xa3video")
    (report / "index.html").write_text("<html><body>passed</body></html>")
    artifacts = [
        {"source": f"indexed/{verification_name}", "destination": f"console/{verification_name}"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
        {"source": f"indexed/{verified_name}", "destination": f"screenshots/{verified_name}"},
        {"source": f"indexed/{configured_name}", "destination": f"screenshots/{configured_name}"},
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


def test_build_checkpoint_seals_exact_signed_audit_evidence(tmp_path: Path) -> None:
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
    assert "blocked_mac_locked" in (destination / "manifest.json").read_text()


def test_build_checkpoint_refuses_unsigned_verification(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)

    with pytest.raises(RuntimeError, match="not a signed intact chain"):
        build_checkpoint(
            source_root=_source(tmp_path / "source", signed=False),
            destination=tmp_path / "sealed",
            repository_root=repository,
        )

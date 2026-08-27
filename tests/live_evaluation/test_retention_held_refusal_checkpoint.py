from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.retention_held_refusal_checkpoint import (
    EXPECTED_CRITERIA,
    build_checkpoint,
)


def _git_repository(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "fixture.txt").write_text("fixture\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)


def _source(root: Path, *, refusal_status: int = 409, chain_verified: bool = True) -> Path:
    indexed = root / "indexed"
    report = root / "html-report"
    indexed.mkdir(parents=True)
    report.mkdir()
    result_name = "aaaaaaaaaaaaaaaa-retention-held-refusal-result.json"
    axe_name = "bbbbbbbbbbbbbbbb-axe-wcag22-aa.json"
    screenshot_names = [
        "cccccccccccccccc-retention-hold-configured.png",
        "dddddddddddddddd-retention-hold-refresh-restored.png",
        "eeeeeeeeeeeeeeee-retention-erasure-staged.png",
        "ffffffffffffffff-retention-held-erasure-refused.png",
        "1111111111111111-retention-refusal-history-restored.png",
        "2222222222222222-retention-hold-released.png",
    ]
    video_names = [
        "3333333333333333-video.webm",
        "4444444444444444-video.webm",
    ]
    result = {
        "tenant_id": "evaluation-studio-v1",
        "run_id": "run-fixture",
        "hold_id": "hold-fixture",
        "refusal_log_id": "log-fixture",
        "refusal_action": "erasure_refused_legal_hold",
        "refusal_status": refusal_status,
        "run_snapshot_unchanged": True,
        "evidence_snapshot_unchanged": True,
        "signed_chain": {
            "verified": chain_verified,
            "signature_verified": chain_verified,
            "unsigned_record_count": 0,
            "record_count": 3,
        },
        "hold_refresh_restored": True,
        "hold_released": True,
        "baseline_hold_ids_preserved": ["baseline-hold"],
        "provider_calls": 0,
        "health": {
            "status": "ok",
            "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
            "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
        },
    }
    (indexed / result_name).write_text(json.dumps(result))
    (indexed / axe_name).write_text("[]")
    for name in screenshot_names:
        (indexed / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    for name in video_names:
        (indexed / name).write_bytes(b"\x1aE\xdf\xa3fixture")
    (report / "index.html").write_text("<html><body>passed</body></html>")
    artifacts = [
        {"source": f"indexed/{result_name}", "destination": f"console/{result_name}"},
        {"source": f"indexed/{axe_name}", "destination": f"accessibility/{axe_name}"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
        *[
            {"source": f"indexed/{name}", "destination": f"screenshots/{name}"}
            for name in screenshot_names
        ],
        *[
            {"source": f"indexed/{name}", "destination": f"videos/{name}"}
            for name in video_names
        ],
    ]
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "criteria": [
                    {
                        "criterion_id": criterion,
                        "status": "pass",
                        "evidence": [row["destination"] for row in artifacts],
                    }
                    for criterion in sorted(EXPECTED_CRITERIA)
                ],
                "artifacts": artifacts,
            }
        )
    )
    return root


def test_build_checkpoint_seals_exact_reversible_refusal_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)
    destination = tmp_path / "sealed"

    build_checkpoint(
        source_root=_source(tmp_path / "source"),
        destination=destination,
        repository_root=repository,
        command_stdout="1 passed",
        command_stderr="",
    )

    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {row["criterion_id"] for row in acceptance["criteria"]} == EXPECTED_CRITERIA
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    assert "provider_calls_performed" in (destination / "manifest.json").read_text()


@pytest.mark.parametrize(
    ("refusal_status", "chain_verified", "message"),
    [
        (200, True, "409"),
        (409, False, "signed intact chain"),
    ],
)
def test_build_checkpoint_rejects_incomplete_runtime_proof(
    tmp_path: Path,
    refusal_status: int,
    chain_verified: bool,
    message: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)

    with pytest.raises(RuntimeError, match=message):
        build_checkpoint(
            source_root=_source(
                tmp_path / "source",
                refusal_status=refusal_status,
                chain_verified=chain_verified,
            ),
            destination=tmp_path / "sealed",
            repository_root=repository,
            command_stdout="1 passed",
            command_stderr="",
        )

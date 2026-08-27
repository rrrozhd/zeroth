from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.cross_cutting_dashboard_checkpoint import (
    EXPECTED_CRITERIA,
    EXPECTED_PROJECTS,
    EXPECTED_ROUTES,
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


def _source(root: Path, *, omit_route: str | None = None) -> Path:
    artifacts = root / "artifacts"
    indexed = root / "indexed"
    report = root / "html-report"
    artifacts.mkdir(parents=True)
    indexed.mkdir()
    report.mkdir()
    result_artifacts: list[dict[str, str]] = [
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"}
    ]
    (report / "index.html").write_text("<html><body>passed</body></html>")
    for project in EXPECTED_PROJECTS:
        project_root = artifacts / f"dashboard-{project}"
        project_root.mkdir()
        for route in EXPECTED_ROUTES:
            if route == omit_route:
                continue
            (project_root / f"{route}-{project}.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
        video_name = f"{project}-video.webm"
        (indexed / video_name).write_bytes(b"\x1aE\xdf\xa3video")
        result_artifacts.append({
            "source": f"indexed/{video_name}",
            "destination": f"videos/{video_name}",
        })
    (root / "results.json").write_text(json.dumps({
        "completed": True,
        "criteria": [
            {"criterion_id": criterion, "status": "pass"}
            for criterion in sorted(EXPECTED_CRITERIA)
        ],
        "artifacts": result_artifacts,
    }))
    return root


def test_build_checkpoint_seals_five_project_route_matrix(tmp_path: Path) -> None:
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
    assert len(list((destination / "screenshots").rglob("*.png"))) == 85


def test_build_checkpoint_refuses_incomplete_route_matrix(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_repository(repository)

    with pytest.raises(RuntimeError, match="screenshot matrix"):
        build_checkpoint(
            source_root=_source(tmp_path / "source", omit_route="audit"),
            destination=tmp_path / "sealed",
            repository_root=repository,
        )

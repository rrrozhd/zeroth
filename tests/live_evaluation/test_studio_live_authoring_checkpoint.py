from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation.evidence import UnsafeEvidenceError
from release.live_evaluation.studio_live_authoring_checkpoint import (
    ACCEPTED_CRITERIA,
    WORKFLOW_ID,
    build_checkpoint,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _source(root: Path) -> Path:
    indexed = root / "indexed"
    indexed.mkdir(parents=True)
    artifacts: list[dict[str, str]] = []
    evidence: list[str] = []

    for name, destination, value in (
        (
            "authoring-runtime.json",
            "console/authoring-runtime.json",
            {
                "workflow_id": WORKFLOW_ID,
                "status": "draft",
                "version": 1,
                "save_shortcut": "Meta+s",
                "node_menu_opened": True,
            },
        ),
        ("sanitized-console.json", "console/sanitized-console.json", []),
        ("sanitized-network.json", "network/sanitized-network.json", {"requests": []}),
    ):
        _write_json(indexed / name, value)
        artifacts.append({"source": f"indexed/{name}", "destination": destination})
        evidence.append(destination)

    for index in range(3):
        name = f"shot-{index}.png"
        (indexed / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        destination = f"screenshots/{name}"
        artifacts.append({"source": f"indexed/{name}", "destination": destination})
        evidence.append(destination)

    (indexed / "video.webm").write_bytes(b"\x1aE\xdf\xa3fixture")
    artifacts.append(
        {"source": "indexed/video.webm", "destination": "videos/video.webm"}
    )
    evidence.append("videos/video.webm")

    report = root / "html-report/index.html"
    report.parent.mkdir()
    report.write_text("<html>safe report</html>\n", encoding="utf-8")
    artifacts.append(
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"}
    )
    _write_json(
        root / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": criterion,
                    "status": "pass",
                    "evidence": evidence,
                }
                for criterion in ACCEPTED_CRITERIA
            ],
            "artifacts": artifacts,
        },
    )
    return root


def _database(path: Path, *, edge_target: str = "node-1") -> Path:
    nodes = [{"node_id": f"node-{index}"} for index in range(6)]
    edges = [
        {
            "edge_id": f"edge-{index}",
            "source_node_id": f"node-{index}",
            "target_node_id": edge_target if index == 0 else f"node-{(index + 1) % 6}",
        }
        for index in range(6)
    ]
    payload = {
        "graph_id": WORKFLOW_ID,
        "name": "Authoring fixture",
        "version": 1,
        "status": "draft",
        "nodes": nodes,
        "edges": edges,
    }
    with sqlite3.connect(path) as database:
        database.execute(
            """
            CREATE TABLE graph_versions (
                graph_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                workspace_id TEXT,
                PRIMARY KEY(graph_id, version)
            )
            """
        )
        database.execute(
            "INSERT INTO graph_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKFLOW_ID,
                1,
                "draft",
                2,
                json.dumps(payload),
                "2026-08-24T00:00:00Z",
                "2026-08-24T00:00:00Z",
                "evaluation-studio-v1",
                None,
            ),
        )
    return path


def _api_workflow(*, edge_target: str = "node-1") -> dict[str, object]:
    return {
        "id": WORKFLOW_ID,
        "name": "Authoring fixture",
        "version": 1,
        "status": "draft",
        "nodes": [{"id": f"node-{index}"} for index in range(6)],
        "edges": [
            {
                "id": f"edge-{index}",
                "source": f"node-{index}",
                "target": edge_target if index == 0 else f"node-{(index + 1) % 6}",
            }
            for index in range(6)
        ],
    }


def test_checkpoint_seals_only_two_passes_after_source_db_api_correlation(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source")
    database = _database(tmp_path / "zeroth.db")
    destination = tmp_path / "checkpoint"

    build_checkpoint(
        destination=destination,
        source_root=source,
        database=database,
        request=lambda path: _api_workflow(),
    )

    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(
        ACCEPTED_CRITERIA
    )
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    assert (destination / "runtime/workflow-correlation.json").is_file()
    assert len(tuple((destination / "screenshots").glob("*.png"))) == 3
    assert len(tuple((destination / "videos").glob("*.webm"))) == 1
    assert (destination / "SHA256SUMS").is_file()


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("extra", "allowlist"),
        ("failed", "allowlist"),
        ("missing-screenshot", "exactly three screenshots"),
    ),
)
def test_checkpoint_rejects_nonexact_source_before_creating_destination(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    source = _source(tmp_path / "source")
    results_path = source / "results.json"
    results = json.loads(results_path.read_text())
    if mutation == "extra":
        results["criteria"].append(
            {"criterion_id": "ui.extra", "status": "pass", "evidence": []}
        )
    elif mutation == "failed":
        results["criteria"][0]["status"] = "fail"
    else:
        results["artifacts"] = [
            row
            for row in results["artifacts"]
            if row["destination"] != "screenshots/shot-2.png"
        ]
    _write_json(results_path, results)
    destination = tmp_path / "checkpoint"

    with pytest.raises(RuntimeError, match=error):
        build_checkpoint(
            destination=destination,
            source_root=source,
            database=_database(tmp_path / "zeroth.db"),
            request=lambda path: _api_workflow(),
        )

    assert not destination.exists()


def test_checkpoint_rejects_secret_shaped_json_before_creating_destination(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source")
    _write_json(source / "indexed/sanitized-console.json", {"api_key": "not-safe"})
    destination = tmp_path / "checkpoint"

    with pytest.raises(UnsafeEvidenceError):
        build_checkpoint(
            destination=destination,
            source_root=source,
            database=_database(tmp_path / "zeroth.db"),
            request=lambda path: _api_workflow(),
        )

    assert not destination.exists()


def test_checkpoint_rejects_db_api_graph_mismatch_before_creating_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "checkpoint"

    with pytest.raises(RuntimeError, match="DB/API graph correlation"):
        build_checkpoint(
            destination=destination,
            source_root=_source(tmp_path / "source"),
            database=_database(tmp_path / "zeroth.db"),
            request=lambda path: _api_workflow(edge_target="node-2"),
        )

    assert not destination.exists()

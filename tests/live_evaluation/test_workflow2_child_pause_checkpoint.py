from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import UnsafeEvidenceError
from release.live_evaluation.provider_free_composed import ITEMS
from release.live_evaluation.workflow2_child_pause_checkpoint import (
    EXPECTED_CRITERIA,
    build_checkpoint,
)

PNG = b"\x89PNG\r\n\x1a\nfixture"
WEBM = b"\x1aE\xdf\xa3fixture"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _outcome(decision: str) -> dict[str, object]:
    parent = f"parent-{decision}"
    before = [
        {
            "run_id": f"{decision}-child-{index}",
            "thread_id": f"{decision}-thread-{index}",
            "parent_run_id": parent,
            "branch_index": index,
            "status": "paused_for_approval" if index == 7 else "succeeded",
        }
        for index in range(8)
    ]
    after = [
        {
            **child,
            "status": (
                "succeeded"
                if child["branch_index"] != 7 or decision == "approve"
                else "failed"
            ),
        }
        for child in before
    ]
    return {
        "decision": decision,
        "reason": f"reviewer {decision} reason",
        "parent_run_id": parent,
        "approval_id": f"approval-{decision}",
        "approval_child_run_id": f"{decision}-child-7",
        "parent_status": "succeeded" if decision == "approve" else "failed",
        "parent_failure_reason": None if decision == "approve" else "parallel_execution_failed",
        "terminal_output": {"items": list(ITEMS)} if decision == "approve" else None,
        "children_before": before,
        "children_after": after,
        "refresh_restored_parent_run_id": parent,
        "refresh_restored_approval_id": f"approval-{decision}",
        "signed_parent_chain": True,
        "signed_child_chain_count": 8,
        "continuation_audit_count": 1,
        "priced_call_count": 0,
        "total_cost_usd": 0,
    }


def _summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_economics_status": "blocked",
        "configured_max_concurrency": 4,
        "approval_branch_index": 7,
        "health": {
            "status": "ok",
            "deployment_ref": "child-pause-parent",
            "graph_version_ref": "child-pause-parent@1",
        },
        "outcomes": [_outcome("approve"), _outcome("reject")],
    }


def _response_identities(summary: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in summary["outcomes"]:  # type: ignore[index]
        outcome = raw  # type: ignore[assignment]
        parent = outcome["parent_run_id"]
        approval = outcome["approval_id"]
        child_ids = [child["run_id"] for child in outcome["children_after"]]
        child_threads = [child["thread_id"] for child in outcome["children_after"]]
        audit_ids = [f"{parent}:branch:{index}:audit:1" for index in range(8)]
        audit_ids.append(f"{parent}:child-approval-continuation:{approval}")
        rows.append(
            {
                "url": f"http://127.0.0.1:8122/v1/runs/{parent}/evidence",
                "status": 200,
                "identity": {
                    "run_id": [parent, *child_ids],
                    "thread_id": [parent, *child_threads],
                    "audit_id": audit_ids,
                    "deployment_ref": ["child-pause-parent", "child-pause-child"],
                    "graph_version_ref": ["child-pause-parent@1", "child-pause-child:v1"],
                    "campaign_id": ["evaluation-studio-v1"],
                },
            }
        )
    return rows


def _source_root(tmp_path: Path, *, include_report_data: bool = True) -> Path:
    source = tmp_path / "source"
    browser = source / "browser"
    indexed = browser / "indexed"
    indexed.mkdir(parents=True)
    summary = _summary()
    _write_json(indexed / "workflow2-child-pause-summary.json", summary)
    _write_json(indexed / "sanitized-console.json", [])
    _write_json(indexed / "response-identities.json", _response_identities(summary))
    _write_json(indexed / "sanitized-network.json", {"requests": [], "responses": []})
    screenshot_names = [
        f"{decision}-{step}.png"
        for decision in ("approve", "reject")
        for step in ("configured", "paused-refresh", "reviewer-decision", "terminal")
    ]
    for name in screenshot_names:
        (indexed / name).write_bytes(PNG)
    (indexed / "video.webm").write_bytes(WEBM)
    report = browser / "html-report"
    report.mkdir()
    (report / "index.html").write_text("<html><body>report</body></html>", encoding="utf-8")
    if include_report_data:
        (report / "data").mkdir()
        (report / "data" / "result.png").write_bytes(PNG)

    artifacts = [
        {
            "source": "indexed/workflow2-child-pause-summary.json",
            "destination": "console/workflow2-child-pause-summary.json",
        },
        {
            "source": "indexed/sanitized-console.json",
            "destination": "console/sanitized-console.json",
        },
        {
            "source": "indexed/response-identities.json",
            "destination": "console/response-identities.json",
        },
        {
            "source": "indexed/sanitized-network.json",
            "destination": "network/sanitized-network.json",
        },
        *(
            {"source": f"indexed/{name}", "destination": f"screenshots/{name}"}
            for name in screenshot_names
        ),
        {"source": "indexed/video.webm", "destination": "videos/video.webm"},
        {"source": "html-report/index.html", "destination": "playwright-report/index.html"},
    ]
    declared = [row["destination"] for row in artifacts if row["destination"] != "playwright-report/index.html"]
    _write_json(
        browser / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": criterion,
                    "status": "pass",
                    "test_id": "child-pause-test",
                    "evidence": declared,
                }
                for criterion in sorted(EXPECTED_CRITERIA)
            ],
            "artifacts": artifacts,
        },
    )
    commands = source / "commands"
    commands.mkdir()
    restored = {
        "status": "ok",
        "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
        "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
    }
    (commands / "live-run.txt").write_text(
        f"{json.dumps(restored)}\nD012_RESTORED\n", encoding="utf-8"
    )
    return source


def _build(tmp_path: Path) -> Path:
    return build_checkpoint(
        source_root=_source_root(tmp_path),
        destination=tmp_path / "sealed",
        repository_root=Path(__file__).resolve().parents[2],
    )


def test_checkpoint_seals_exact_identities_and_marks_snapshot_not_run(tmp_path: Path) -> None:
    root = _build(tmp_path)

    acceptance = json.loads((root / "acceptance.json").read_text(encoding="utf-8"))
    assert {row["criterion_id"]: row["status"] for row in acceptance["criteria"]} == {
        **{criterion: "pass" for criterion in EXPECTED_CRITERIA},
        "evidence.database-snapshots": "not_run",
    }
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert [row["parent_run_id"] for row in manifest["execution_identities"]] == [
        "parent-approve",
        "parent-reject",
    ]
    assert all(len(row["child_run_ids"]) == 8 for row in manifest["execution_identities"])
    assert all(len(row["audit_ids"]) == 9 for row in manifest["execution_identities"])
    assert all(row["cost_event_ids"] == [] for row in manifest["execution_identities"])
    assert (root / "playwright-report/data/result.png").is_file()
    assert not list(root.rglob("*.sqlite*"))
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


def test_checkpoint_rejects_missing_exact_browser_criterion(tmp_path: Path) -> None:
    source = _source_root(tmp_path)
    results_path = source / "browser/results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    results["criteria"].pop()
    _write_json(results_path, results)

    with pytest.raises(RuntimeError, match="exact allowlist"):
        build_checkpoint(
            source_root=source,
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
        )


def test_checkpoint_rejects_missing_continuation_audit_identity(tmp_path: Path) -> None:
    source = _source_root(tmp_path)
    identities_path = source / "browser/indexed/response-identities.json"
    identities = json.loads(identities_path.read_text(encoding="utf-8"))
    identities[0]["identity"]["audit_id"].pop()
    _write_json(identities_path, identities)

    with pytest.raises(RuntimeError, match="audit identities"):
        build_checkpoint(
            source_root=source,
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
        )


def test_checkpoint_rejects_incomplete_html_report(tmp_path: Path) -> None:
    source = _source_root(tmp_path, include_report_data=False)
    with pytest.raises(RuntimeError, match="full Playwright HTML report"):
        build_checkpoint(
            source_root=source,
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
        )


def test_checkpoint_rejects_secret_shaped_indexed_artifact(tmp_path: Path) -> None:
    source = _source_root(tmp_path)
    _write_json(source / "browser/indexed/sanitized-console.json", {"api_key": "unsafe"})
    with pytest.raises(UnsafeEvidenceError):
        build_checkpoint(
            source_root=source,
            destination=tmp_path / "sealed",
            repository_root=Path(__file__).resolve().parents[2],
        )

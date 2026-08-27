from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow1_excessive_revision_checkpoint import build_checkpoint


DEPLOYMENT = "provider-free-w1-excessive-revision-w1-revision-20260826a"
GRAPH = "workflow-1-revision@1"
RUN_ID = "a" * 32


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _source(tmp_path: Path, *, browser_status: str = "pass") -> Path:
    source = tmp_path / "source"
    summary = {
        "schema_version": 1,
        "health": {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
        },
        "run": {
            "run_id": RUN_ID,
            "thread_id": RUN_ID,
            "status": "terminated_by_loop_guard",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "failure_reason": "max_total_steps",
            "research_visit_count": 2,
            "node_visit_counts": {"request": 1, "research": 2, "revision-loop": 1},
        },
        "timeline": {
            "node_ids": ["request", "research", "revision-loop", "research"],
            "research_visit_count": 2,
        },
        "audit": {
            "verified": True,
            "signature_verified": True,
            "record_count": 4,
            "unsigned_record_count": 0,
            "audit_ids": [f"audit-{index}" for index in range(1, 5)],
            "research_audit_ids": ["audit-2", "audit-4"],
        },
        "economics": {
            "provider_calls_performed": 0,
            "provider_request_ids": [],
            "cost_event_ids": [],
            "priced_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "cost_identity_state": "not_applicable_no_priced_call",
            "reconciliation_state": "reconciled_zero_activity",
        },
        "refresh": {
            "before_run_id": RUN_ID,
            "restored_run_id": RUN_ID,
            "restored_status": "terminated_by_loop_guard",
            "restored_failure_reason": "max_total_steps",
            "restored_research_visit_count": 2,
        },
        "d012_restore": {
            "before": {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
            "after": {
                "status": "ok",
                "campaign_id": "evaluation-studio-v1",
                "deployment_ref": "provider-free-child-approval-d012-live-2-parent",
                "deployment_version": 1,
                "graph_version_ref": "d012-parent@1",
            },
            "exact": True,
        },
    }
    _write(source / "runtime/summary.json", summary)
    browser = source / "browser"
    indexed = browser / "indexed"
    indexed.mkdir(parents=True)
    artifacts = []
    for index in range(1, 4):
        name = f"screen-{index}.png"
        (indexed / name).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
        artifacts.append({"source": f"indexed/{name}", "destination": f"screenshots/{name}"})
    (indexed / "run.webm").write_bytes(b"\x1aE\xdf\xa3safe")
    artifacts.append({"source": "indexed/run.webm", "destination": "videos/run.webm"})
    for name, payload in (
        ("sanitized-network.json", {"requests": [], "responses": []}),
        ("sanitized-console.json", []),
        ("response-identities.json", []),
    ):
        _write(indexed / name, payload)
        category = "network" if "network" in name else "console"
        artifacts.append({"source": f"indexed/{name}", "destination": f"{category}/{name}"})
    report = browser / "html-report/index.html"
    report.parent.mkdir(parents=True)
    report.write_text("safe report", encoding="utf-8")
    artifacts.append({"source": "html-report/index.html", "destination": "playwright-report/index.html"})
    _write(
        browser / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": "workflow1.negative-excessive-revision",
                    "status": browser_status,
                    "test_id": "exact-ui-test",
                    "evidence": [item["destination"] for item in artifacts],
                }
            ],
            "artifacts": artifacts,
        },
    )
    return source


def test_checkpoint_seals_only_exact_excessive_revision_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "sealed"

    build_checkpoint(source_root=_source(tmp_path), destination=destination)

    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"] == [
        {
            "criterion_id": "workflow1.negative-excessive-revision",
            "status": "pass",
            "evidence": acceptance["criteria"][0]["evidence"],
            "note": None,
        }
    ]
    assert (destination / "screenshots/screen-3.png").is_file()
    assert (destination / "videos/run.webm").is_file()
    assert (destination / "runtime/summary.json").is_file()


def test_checkpoint_refuses_failed_browser_attempt(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="accepted criterion"):
        build_checkpoint(
            source_root=_source(tmp_path, browser_status="fail"),
            destination=tmp_path / "unsealed",
        )

    assert not (tmp_path / "unsealed").exists()

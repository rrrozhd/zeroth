from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module(
        "release.live_evaluation.native_safari_loop_refresh_checkpoint"
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _source(tmp_path: Path, *, restored_run_id: str | None = None) -> Path:
    run_id = "a" * 32
    source = tmp_path / "source"
    runtime = source / "runtime"
    _write_json(
        runtime / "health.json",
        {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": "demo-data-quality-repair-loop",
            "deployment_version": 1,
            "graph_version_ref": "06a4c062-5134-4066-a286-cf9da0109b39@1",
        },
    )
    _write_json(
        runtime / "run.json",
        {
            "run_id": run_id,
            "thread_id": run_id,
            "status": "succeeded",
            "deployment_ref": "demo-data-quality-repair-loop",
            "graph_version_ref": "06a4c062-5134-4066-a286-cf9da0109b39@1",
            "campaign_id": "evaluation-studio-v1",
            "terminal_output": {
                "quality_score": 1.0,
                "repair_pass": 0,
                "needs_repair": False,
                "quality_issues": [],
                "result": {
                    "quality_score": 1.0,
                    "remaining_issues": 0,
                    "repair_passes": 0,
                    "status": "ready",
                },
            },
            "failure_state": None,
            "audit_refs": ["audit:1", "audit:2", "audit:3"],
        },
    )
    nodes = ["start", "inspect", "finalize"]
    _write_json(
        runtime / "timeline.json",
        {
            "entries": [
                {
                    "audit_id": f"{run_id}:audit:{index}",
                    "run_id": run_id,
                    "thread_id": run_id,
                    "node_id": node,
                    "status": "completed",
                    "deployment_ref": "demo-data-quality-repair-loop",
                    "graph_version_ref": "06a4c062-5134-4066-a286-cf9da0109b39@1",
                    "cost_usd": 0.0,
                    "estimated_cost_usd": 0.0,
                    "cost_event_id": None,
                }
                for index, node in enumerate(nodes, start=1)
            ]
        },
    )
    _write_json(
        runtime / "audits.json",
        {
            "records": [
                {
                    "audit_id": f"{run_id}:audit:{index}",
                    "run_id": run_id,
                    "node_id": node,
                    "status": "completed",
                    "deployment_ref": "demo-data-quality-repair-loop",
                    "graph_version_ref": "06a4c062-5134-4066-a286-cf9da0109b39@1",
                    "chain_sequence": index,
                    "record_digest": str(index) * 64,
                    "record_signature": str(index + 3) * 64,
                    "signing_key_id": "dev-local",
                    "signing_algorithm": "HS256",
                    "cost_usd": 0.0,
                    "estimated_cost_usd": 0.0,
                    "cost_event_id": None,
                }
                for index, node in enumerate(nodes, start=1)
            ]
        },
    )
    _write_json(
        runtime / "run-audit-verification.json",
        {
            "scope": f"run:{run_id}",
            "verified": True,
            "signature_verified": True,
            "record_count": 3,
            "unsigned_record_count": 0,
            "signing_key_id": "dev-local",
            "failed_audit_id": None,
            "error": None,
        },
    )
    _write_json(
        runtime / "run-evidence.json",
        {
            "run": {"run_id": run_id},
            "audits": [{"node_id": node} for node in nodes],
            "approvals": [],
            "policy_events": [],
            "summary": {
                "audit_count": 3,
                "approval_count": 0,
                "tool_call_count": 0,
                "memory_interaction_count": 0,
                "priced_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
                "cost_identity_state": "not_applicable_no_priced_call",
                "reconciliation_state": "reconciled_zero_activity",
            },
        },
    )
    _write_json(
        runtime / "deployment-cost.json",
        {
            "deployment_ref": "demo-data-quality-repair-loop",
            "total_cost_usd": 0.0,
            "paid_spend_usd": 0.0,
            "estimated_spend_usd": 0.0,
            "unmeasured_spend_usd": 0.0,
            "active_exposure_usd": 0.0,
            "ambiguous_exposure_usd": 0.0,
            "currency": "USD",
        },
    )
    screenshot_root = source / "screenshots"
    screenshot_root.mkdir(parents=True)
    for name in (
        "01-loop-succeeded-before-refresh-native-safari.jpg",
        "02-loop-succeeded-after-refresh-native-safari.jpg",
    ):
        (screenshot_root / name).write_bytes(b"\xff\xd8\xffsafe-jpeg")
    accessibility_root = source / "accessibility"
    accessibility_root.mkdir(parents=True)
    (accessibility_root / "01-loop-succeeded-before-refresh-native-safari.txt").write_text(
        f"Run Succeeded\ntext {run_id}\n", encoding="utf-8"
    )
    (accessibility_root / "02-loop-succeeded-after-refresh-native-safari.txt").write_text(
        f"Run Succeeded\ntext {restored_run_id or run_id}\n", encoding="utf-8"
    )
    return source


def test_checkpoint_seals_exact_native_safari_loop_refresh_identity(
    tmp_path: Path,
) -> None:
    module = _module()
    destination = tmp_path / "sealed"

    result = module.build_checkpoint(
        source_root=_source(tmp_path), destination=destination
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(
        module.ACCEPTED_CRITERIA
    )
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["run_id"] == "a" * 32
    assert manifest["native_safari_screenshot_count"] == 2
    assert manifest["provider_calls_performed"] == 0
    assert (destination / "screenshots/02-loop-succeeded-after-refresh-native-safari.jpg").is_file()
    assert (destination / "accessibility/02-loop-succeeded-after-refresh-native-safari.txt").is_file()


def test_checkpoint_rejects_a_relabelled_post_refresh_run_identity(
    tmp_path: Path,
) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="same run identity"):
        module.build_checkpoint(
            source_root=_source(tmp_path, restored_run_id="b" * 32),
            destination=tmp_path / "bad",
        )


def test_checkpoint_rejects_nonzero_or_unreconciled_economics(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    cost_path = source / "runtime/deployment-cost.json"
    cost = json.loads(cost_path.read_text())
    cost["total_cost_usd"] = 0.01
    cost_path.write_text(json.dumps(cost), encoding="utf-8")

    with pytest.raises(RuntimeError, match="zero-activity economics"):
        module.build_checkpoint(source_root=source, destination=tmp_path / "bad")

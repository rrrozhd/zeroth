from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow1_provider_faults_checkpoint import (
    ACCEPTED_CRITERIA,
    build_checkpoint,
)
from release.live_evaluation.workflow1_provider_faults_live import EXPECTED_MODES


def _summary() -> dict[str, object]:
    cases = []
    for index, mode in enumerate(EXPECTED_MODES, start=1):
        run_id = f"{index:x}" * 32
        cases.append(
            {
                "mode": mode,
                "fault_id": f"{index + 3:x}" * 32,
                "fault_consumed": True,
                "run_id": run_id,
                "status": "failed",
                "failure_reason": "node_execution_failed",
                "timeline_node_ids": ["request", "answer"],
                "timeline_statuses": ["completed", "failed"],
                "audit_verified": True,
                "signature_verified": True,
                "audit_record_count": 2,
                "unsigned_record_count": 0,
                "provider_request_ids": [],
                "cost_event_ids": [],
                "priced_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
                "cost_identity_state": "not_applicable_no_priced_call",
                "reconciliation_state": "reconciled_zero_activity",
                "refresh": {
                    "before_run_id": run_id,
                    "restored_run_id": run_id,
                    "restored_status": "failed",
                },
            }
        )
    return {
        "schema_version": 1,
        "deployment_ref": "provider-free-w1-provider-faults-test",
        "graph_version_ref": "workflow-provider-faults@1",
        "provider_calls_performed": 0,
        "cases": cases,
        "d012_restore": {
            "exact": True,
            "before": {"deployment_ref": "d012"},
            "after": {"deployment_ref": "d012"},
        },
    }


def _source(tmp_path: Path, *, screenshot_count: int = 9) -> Path:
    source = tmp_path / "source"
    (source / "runtime").mkdir(parents=True)
    (source / "runtime/summary.json").write_text(
        json.dumps(_summary()), encoding="utf-8"
    )
    browser = source / "browser"
    artifacts: list[dict[str, str]] = []
    for index in range(screenshot_count):
        relative = Path("screenshots") / f"shot-{index}.png"
        path = browser / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # The evidence gate reads only the signed PNG header dimensions; the
        # artifact scanner independently validates the declared media signature.
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 1440, 900)
            + b"safe"
        )
        artifacts.append({"source": relative.as_posix(), "destination": relative.as_posix()})
    for prefix in ("network", "handoff"):
        relative = Path(prefix) / "summary.json"
        path = browser / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"safe"}\n', encoding="utf-8")
        artifacts.append({"source": relative.as_posix(), "destination": relative.as_posix()})
    (browser / "evidence-index.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
    )
    return source


def test_checkpoint_seals_all_four_exact_criteria(tmp_path: Path) -> None:
    destination = tmp_path / "sealed"
    build_checkpoint(source_root=_source(tmp_path), destination=destination)

    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(
        ACCEPTED_CRITERIA
    )
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    assert len(list((destination / "screenshots").glob("*.png"))) == 9
    assert "does not claim live-provider execution" in (
        destination / "report.md"
    ).read_text()


def test_checkpoint_fails_closed_without_nine_screenshots(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="nine screenshots"):
        build_checkpoint(
            source_root=_source(tmp_path, screenshot_count=8),
            destination=tmp_path / "sealed",
        )


def test_checkpoint_fails_closed_on_wrong_viewport(tmp_path: Path) -> None:
    source = _source(tmp_path)
    screenshot = source / "browser/screenshots/shot-0.png"
    payload = screenshot.read_bytes()
    screenshot.write_bytes(payload[:16] + struct.pack(">II", 1280, 800) + payload[24:])

    with pytest.raises(RuntimeError, match="1440x900"):
        build_checkpoint(source_root=source, destination=tmp_path / "sealed")

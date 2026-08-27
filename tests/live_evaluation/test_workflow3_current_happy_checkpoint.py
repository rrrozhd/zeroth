from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.workflow3_current_happy_checkpoint import (
    ACCEPTED_CRITERIA,
    MANIFEST_REF_SHA256,
    _load_ui_source,
    _validate_run_proof,
)


def _proof() -> dict[str, object]:
    digests = [f"{'a' * 63}{index}" for index in range(1, 5)]
    return {
        "run_id": "run-1",
        "runtime": {
            "status": "succeeded",
            "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
            "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
            "tenant_id": "evaluation-studio-v1",
            "campaign_id": "evaluation-studio-v1",
            "operation_key": "op-1",
            "payload_hash": "b" * 64,
            "receipt_sha256": "c" * 64,
        },
        "verification": {
            "verified": True,
            "signature_verified": True,
            "record_count": 4,
            "unsigned_record_count": 0,
        },
        "approval": {"count": 1, "status": "resolved", "decision": "approve"},
        "audits": [
            {
                "audit_id": f"audit-{index}",
                "node_id": node,
                "chain_sequence": index,
                "record_digest": digests[index - 1],
                "previous_record_digest": None if index == 1 else digests[index - 2],
                "record_signature_present": True,
                "cost_usd": 0.0,
                "cost_event_id": None,
                "operation_key": "op-1" if index == 4 else None,
                "manifest_ref_sha256": (MANIFEST_REF_SHA256 if index == 4 else None),
                "operation_state": "completed" if index == 4 else None,
            }
            for index, node in enumerate(
                ("request", "approval", "approval", "synthetic-action"), start=1
            )
        ],
        "operation": {
            "count": 1,
            "operation_key": "op-1",
            "target_ref": "evaluation://synthetic-action/v1",
            "state": "COMPLETED",
            "payload_hash": "b" * 64,
            "receipt_sha256": "c" * 64,
        },
        "marker": {
            "count": 1,
            "operation_key": "op-1",
            "payload_hash": "b" * 64,
            "receipt_sha256": "c" * 64,
        },
        "manifest": {
            "side_effect": True,
            "execution_placement": "local_only",
            "content_hash": "e" * 64,
            "manifest_ref_sha256": MANIFEST_REF_SHA256,
            "run_linked": True,
        },
        "economics": {
            "execution_event_count": 0,
            "reservation_count": 0,
            "execution_cost_usd": 0.0,
            "reservation_actual_cost_usd": 0.0,
            "audit_cost_usd": 0.0,
            "provider_identity_count": 0,
        },
    }


def test_run_proof_requires_exact_operation_marker_digest_and_zero_cost() -> None:
    _validate_run_proof(_proof())

    duplicate = _proof()
    duplicate["marker"]["count"] = 2  # type: ignore[index]
    with pytest.raises(RuntimeError, match="exactly one action marker"):
        _validate_run_proof(duplicate)

    broken = _proof()
    broken["audits"][3]["previous_record_digest"] = "f" * 64  # type: ignore[index]
    with pytest.raises(RuntimeError, match="audit digest chain"):
        _validate_run_proof(broken)

    charged = _proof()
    charged["economics"]["execution_cost_usd"] = 0.01  # type: ignore[index]
    with pytest.raises(RuntimeError, match="zero provider/economics cost"):
        _validate_run_proof(charged)


def test_ui_source_requires_three_runs_twelve_screenshots_and_exact_criteria(
    tmp_path: Path,
) -> None:
    indexed = tmp_path / "indexed"
    indexed.mkdir(parents=True)
    run_file = indexed / "safe-workflow3-current-happy-runs.json"
    run_file.write_text(
        json.dumps(
            {
                "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
                "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
                "manifest_content_hash": "e" * 64,
                "runs": [{"repetition": index, "run_id": f"run-{index}"} for index in range(1, 4)],
            }
        )
    )
    for index in range(12):
        (indexed / f"shot-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    for name in ("sanitized-console.json", "sanitized-network.json", "response-identities.json"):
        (indexed / name).write_text("{}\n")
    for index in range(2):
        (indexed / f"video-{index}.webm").write_bytes(b"\x1aE\xdf\xa3fixture")
    report = tmp_path / "html-report"
    report.mkdir()
    (report / "index.html").write_text("<html>safe</html>")
    (tmp_path / "results.json").write_text(
        json.dumps(
            {
                "completed": True,
                "criteria": [
                    {"criterion_id": criterion, "status": "pass"} for criterion in ACCEPTED_CRITERIA
                ],
            }
        )
    )

    source = _load_ui_source(tmp_path)

    assert source.run_ids == ("run-1", "run-2", "run-3")
    assert len(source.screenshots) == 12
    assert len(source.videos) == 2

    (indexed / "shot-11.png").unlink()
    with pytest.raises(RuntimeError, match="exactly 12 screenshots"):
        _load_ui_source(tmp_path)


def test_acceptance_allowlist_is_exactly_the_four_requested_criteria() -> None:
    assert ACCEPTED_CRITERIA == (
        "workflow3.signed-action-sink-registered",
        "workflow3.exactly-one-marker-each",
        "audit.approval-action-linkage",
        "audit.receipts-linked",
    )

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.campaign_finalizer import EvidenceFirstCampaignFinalizer
from release.live_evaluation.criteria import original_acceptance_criteria
from release.live_evaluation.evidence import EvidenceStore, UnsafeEvidenceError
from release.live_evaluation.ledger import CampaignLedger


FINAL_IDS = {
    "evidence.acceptance",
    "evidence.report",
    "evidence.sha256-checksums",
}


def _ready_ledger(tmp_path: Path) -> tuple[EvidenceStore, CampaignLedger]:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123", "dirty_tree_hash": "sha256:" + "a" * 64})
    store.append_event("campaign.fixture.ready", {"status": "safe"}, event_id="witness")
    ledger = CampaignLedger(store, original_acceptance_criteria())
    for item in original_acceptance_criteria():
        if item.criterion_id not in FINAL_IDS:
            ledger.record(item.criterion_id, "pass", evidence=("events.ndjson#witness",))
    return store, ledger


def test_finalizer_publishes_full_report_acceptance_and_verified_seal(tmp_path: Path) -> None:
    store, ledger = _ready_ledger(tmp_path)

    EvidenceFirstCampaignFinalizer().finalize(store=store, ledger=ledger)

    acceptance = json.loads((tmp_path / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == [
        item.criterion_id for item in original_acceptance_criteria()
    ]
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    report = (tmp_path / "report.md").read_text()
    assert "# Full Evidence-First Live Evaluation" in report
    assert "## Event inventory" in report
    assert "handoff.discrepancy-register" in report
    assert "sk-proj-" not in report
    final_events = store.read_events()
    acceptance_event_count = sum(
        event["type"] == "acceptance.recorded" for event in final_events
    )
    assert f"| `acceptance.recorded` | {acceptance_event_count} |" in report
    assert "| `campaign.finalization.ready` | 1 |" in report
    for line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest() == digest
    with pytest.raises(RuntimeError, match="finalized"):
        ledger.record("workflow1.happy-1", "pass", evidence=("events.ndjson#witness",))


def test_finalizer_refuses_incomplete_catalog_without_creating_final_files(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    store.append_event("campaign.fixture.ready", {"status": "safe"}, event_id="witness")
    ledger = CampaignLedger(store, original_acceptance_criteria())
    ledger.record("workflow1.happy-1", "pass", evidence=("events.ndjson#witness",))

    with pytest.raises(RuntimeError, match="not all passing"):
        EvidenceFirstCampaignFinalizer().finalize(store=store, ledger=ledger)

    assert not (tmp_path / "acceptance.json").exists()
    assert not (tmp_path / "report.md").exists()
    assert not (tmp_path / "SHA256SUMS").exists()
    assert not any(
        event["type"] == "campaign.finalization.ready" for event in store.read_events()
    )


def test_finalizer_scans_existing_artifacts_before_recording_derived_passes(
    tmp_path: Path,
) -> None:
    store, ledger = _ready_ledger(tmp_path)
    unsafe = tmp_path / "console" / "unsafe.json"
    unsafe.parent.mkdir()
    unsafe.write_text('{"authorization":"must-not-survive"}\n')

    with pytest.raises(UnsafeEvidenceError):
        EvidenceFirstCampaignFinalizer().finalize(store=store, ledger=ledger)

    assert not store.is_sealed
    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert all(statuses[item] == "not_run" for item in FINAL_IDS)


def test_finalizer_rejects_a_ledger_from_another_bundle(tmp_path: Path) -> None:
    store, _ = _ready_ledger(tmp_path / "one")
    _, other_ledger = _ready_ledger(tmp_path / "two")

    with pytest.raises(ValueError, match="different evidence bundle"):
        EvidenceFirstCampaignFinalizer().finalize(store=store, ledger=other_ledger)

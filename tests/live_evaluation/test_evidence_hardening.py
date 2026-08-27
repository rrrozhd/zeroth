from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.control_plane import ControlPlaneEvidence
from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
    UnsafeEvidenceError,
)
from release.live_evaluation.ledger import CampaignLedger


def _catalog() -> tuple[AcceptanceCriterion, ...]:
    return (
        AcceptanceCriterion("workflow1.happy-1", "not_run"),
        AcceptanceCriterion("stop.no-secret-artifact", "not_run"),
    )


def test_ledger_resumes_recorded_state_from_durable_events(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append_event("witness", {"result": "ok"}, event_id="witness-1")
    first = CampaignLedger(store, _catalog())
    first.record(
        "workflow1.happy-1",
        "pass",
        evidence=("events.ndjson#witness-1",),
    )

    resumed = CampaignLedger(EvidenceStore(tmp_path), _catalog())

    assert resumed.criteria[0].status == "pass"
    with pytest.raises(ValueError, match="already recorded"):
        resumed.record(
            "workflow1.happy-1",
            "pass",
            evidence=("events.ndjson#witness-1",),
        )


def test_control_plane_finalizes_ledger_once_without_acceptance_collision(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    store.append_event("witness", {"result": "ok"}, event_id="witness-1")
    control = ControlPlaneEvidence(store=store, acceptance_catalog=_catalog())
    ledger = control.resume_ledger()
    ledger.record(
        "workflow1.happy-1",
        "pass",
        evidence=("events.ndjson#witness-1",),
    )

    control.finalize(report_markdown="# Report\n", ledger=ledger)

    acceptance = json.loads((tmp_path / "acceptance.json").read_text())
    assert acceptance["criteria"][0]["status"] == "pass"
    assert store.is_sealed
    with pytest.raises(RuntimeError, match="sealed"):
        store.append_event("too-late", {"result": "no"})
    with pytest.raises(RuntimeError, match="sealed"):
        store.write_report("replacement")


def test_finalization_resumes_byte_identical_unsealed_partial_publish(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    control = ControlPlaneEvidence(store=store, acceptance_catalog=_catalog())
    ledger = control.resume_ledger()
    store.write_acceptance(ledger.resolved_criteria())

    control.finalize(report_markdown="# Recovered report\n", ledger=ledger)

    assert (tmp_path / "report.md").read_text() == "# Recovered report\n"
    assert store.is_sealed


def test_finalization_rejects_missing_and_unknown_event_evidence(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    control = ControlPlaneEvidence(store=store, acceptance_catalog=_catalog())
    ledger = control.resume_ledger()
    ledger.record(
        "workflow1.happy-1",
        "pass",
        evidence=("events.ndjson#missing-event",),
    )

    with pytest.raises(ValueError, match="evidence reference"):
        control.finalize(report_markdown="# Report\n", ledger=ledger)

    assert not (tmp_path / "acceptance.json").exists()
    assert not (tmp_path / "report.md").exists()
    assert not store.is_sealed


def test_ingestion_enforces_destination_and_binary_policy(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-image")
    store = EvidenceStore(tmp_path / "bundle")

    ingested = store.ingest_artifact(source, "screenshots/workflow-1.png")

    assert ingested.read_bytes() == source.read_bytes()
    with pytest.raises(ValueError, match="artifact destination"):
        store.ingest_artifact(source, "../escaped.png")
    unknown = tmp_path / "capture.bin"
    unknown.write_bytes(b"opaque")
    with pytest.raises(ValueError, match="artifact type"):
        store.ingest_artifact(unknown, "screenshots/capture.bin")


def test_final_recursive_scan_blocks_secret_in_tampered_artifact(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    store.append_event("witness", {"result": "ok"}, event_id="witness-1")
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()
    (screenshot_dir / "tampered.txt").write_text(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    )
    control = ControlPlaneEvidence(store=store, acceptance_catalog=_catalog())
    ledger = control.resume_ledger()
    ledger.record(
        "workflow1.happy-1",
        "pass",
        evidence=("events.ndjson#witness-1",),
    )

    with pytest.raises(UnsafeEvidenceError):
        control.finalize(report_markdown="# Report\n", ledger=ledger)

    assert not (tmp_path / "acceptance.json").exists()
    assert not store.is_sealed


def test_final_recursive_scan_understands_secret_json_fields(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    artifact_dir = tmp_path / "network"
    artifact_dir.mkdir()
    (artifact_dir / "tampered.json").write_text(
        '{"provider_key":"credential-that-must-not-survive"}\n'
    )

    with pytest.raises(UnsafeEvidenceError):
        store.write_checksums()

    assert not store.is_sealed


def test_final_recursive_scan_rejects_artifacts_that_bypass_ingestion_policy(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    artifact_dir = tmp_path / "screenshots"
    artifact_dir.mkdir()
    (artifact_dir / "opaque.bin").write_bytes(b"opaque but not an approved artifact")

    with pytest.raises(UnsafeEvidenceError, match="artifact type"):
        store.write_checksums()


def test_failed_event_append_does_not_advance_in_memory_ledger(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write_checksums()
    ledger = CampaignLedger(store, _catalog())

    with pytest.raises(RuntimeError, match="sealed"):
        ledger.record(
            "workflow1.happy-1",
            "pass",
            evidence=("events.ndjson#witness-1",),
        )

    assert ledger.criteria[0].status == "not_run"


def test_command_storage_rejects_path_traversal_name(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "bundle")

    with pytest.raises(ValueError, match="safe slug"):
        store.record_command(
            sequence=1,
            name="../../escaped",
            argv=("safe",),
            working_directory=tmp_path,
            exit_code=0,
            stdout="",
            stderr="",
        )

    assert not (tmp_path / "escaped.json").exists()


def test_correlations_are_typed_and_cannot_alias_identity_namespaces(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(ValueError, match="distinct"):
        CorrelationIds(operation_id="same-id", run_id="same-id")
    with pytest.raises(ValueError, match="typed correlation"):
        store.append_event(
            "provider.completed",
            {"provider_request_id": "untyped-request"},
        )
    with pytest.raises(ValueError, match="typed correlation"):
        store.append_event(
            "campaign.api.completed",
            {"metadata": {"operation_id": "hidden-untyped-operation"}},
        )


def test_provider_ui_and_cost_events_require_canonical_correlation_ids(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    scope = CorrelationIds(operation_id="op-1", run_id="run-1")

    with pytest.raises(ValueError, match="provider_request_id"):
        store.append_event("provider.completed", {}, correlation=scope)
    with pytest.raises(ValueError, match="cost_event_id"):
        store.append_event("campaign.cost.committed", {}, correlation=scope)
    with pytest.raises(ValueError, match="ui_action_id"):
        store.append_event("campaign.ui.completed", {}, correlation=scope)


def test_correlation_entity_cannot_change_operation_scope(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append_event(
        "provider.completed",
        {},
        correlation=CorrelationIds(
            operation_id="op-1",
            run_id="run-1",
            provider_request_id="provider-request-1",
        ),
    )

    with pytest.raises(ValueError, match="conflicting operation_id"):
        store.append_event(
            "provider.reconciled",
            {},
            correlation=CorrelationIds(
                operation_id="op-2",
                run_id="run-1",
                provider_request_id="provider-request-1",
            ),
        )


@pytest.mark.parametrize("reference", ("acceptance.json", "./report.md", "SHA256SUMS"))
def test_final_criteria_cannot_use_final_files_as_their_own_evidence(
    tmp_path: Path, reference: str
) -> None:
    store = EvidenceStore(tmp_path)
    (tmp_path / Path(reference).name).write_text("{}\n")
    criterion = AcceptanceCriterion(
        "workflow1.happy-1", "pass", evidence=(reference,)
    )

    with pytest.raises(ValueError, match="final-file evidence reference"):
        store.validate_evidence_references((criterion,))

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.live_sources import (
    BoundedPlaywrightProducer,
    BoundedSnapshotProducer,
    BoundedZerothCheckRunner,
    CampaignSnapshotCollector,
    ReconciliationCollectionBlocked,
)
from release.live_evaluation.reconciliation import ReconciliationResult


def _snapshot(campaign_id: str, tenant_id: str) -> dict[str, object]:
    tagged = {"campaign_id": campaign_id, "tenant_id": tenant_id}
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "tenant_id": tenant_id,
        "audits": [
            {
                **tagged,
                "audit_event_id": "audit-1",
                "operation_id": "op-1",
                "run_id": "run-1",
                "cost_event_id": "cost-1",
                "provider_request_id": "provider-1",
                "cost_usd": "0.01",
                "cache_hit": False,
                "run_status": "succeeded",
                "signed": True,
                "chain_verified": True,
            }
        ],
        "reservations": [
            {
                **tagged,
                "reservation_id": "reservation-1",
                "operation_id": "op-1",
                "run_id": "run-1",
                "state": "committed",
                "maximum_usd": "0.02",
                "retained_usd": "0.01",
            }
        ],
        "local_cost_events": [
            {
                **tagged,
                "cost_event_id": "cost-1",
                "audit_event_id": "audit-1",
                "operation_id": "op-1",
                "run_id": "run-1",
                "provider_request_id": "provider-1",
                "amount_usd": "0.01",
                "cache_hit": False,
                "run_status": "succeeded",
                "failure_tax_usd": "0",
            }
        ],
        "regulus_events": [
            {
                **tagged,
                "execution_event_id": "execution-1",
                "cost_event_id": "cost-1",
                "audit_event_id": "audit-1",
                "operation_id": "op-1",
                "run_id": "run-1",
                "provider_request_id": "provider-1",
                "amount_usd": "0.01",
                "failure_tax_usd": "0",
                "valuation_recorded": False,
                "value_usd": "0",
                "margin_usd": "0",
                "synthetic_outcome_id": None,
            }
        ],
        "action_receipts": [],
        "excluded_reservations": [],
        "provider_window": {"window_id": "window-1", "total_usd": "0.02"},
    }


def test_snapshot_collector_requires_per_record_tags_and_ingests_source(
    tmp_path: Path,
) -> None:
    campaign_id = "evaluation-studio-v1"
    tenant_id = "tenant-evaluation"
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_snapshot(campaign_id, tenant_id)))
    seen = []

    def reconcile(store, snapshot):
        seen.append(snapshot)
        return ReconciliationResult(False, *([__import__("decimal").Decimal("0")] * 4), (), ())

    store = EvidenceStore(tmp_path / "bundle")
    result = CampaignSnapshotCollector(
        source=source,
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        reconciler=reconcile,
    )(store)

    assert result is not None
    assert seen[0].audits[0].operation_id == "op-1"
    assert (store.root / "reconciliation/input.json").is_file()


def test_snapshot_collector_preserves_proven_excluded_reservations(tmp_path: Path) -> None:
    payload = _snapshot("evaluation-studio-v1", "tenant-evaluation")
    payload["excluded_reservations"] = [
        {
            "campaign_id": "evaluation-studio-v1",
            "tenant_id": "tenant-evaluation",
            "reservation_id": "reservation-2",
            "operation_id": "op-2",
            "run_id": "run-1",
            "reason": "provider_not_called",
            "cleanup_status": "provider_not_called",
        }
    ]
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(payload))

    CampaignSnapshotCollector(
        source=source,
        campaign_id="evaluation-studio-v1",
        tenant_id="tenant-evaluation",
        reconciler=lambda _store, _snapshot: ReconciliationResult(
            False, *([__import__("decimal").Decimal("0")] * 4), (), ()
        ),
    )(EvidenceStore(tmp_path / "bundle"))

    ingested = json.loads(
        (tmp_path / "bundle/reconciliation/input.json").read_text(encoding="utf-8")
    )
    assert ingested["excluded_reservations"][0]["reason"] == "provider_not_called"


def test_snapshot_collector_blocks_mixed_campaign_rows(tmp_path: Path) -> None:
    payload = _snapshot("evaluation-studio-v1", "tenant-evaluation")
    payload["audits"][0]["campaign_id"] = "evaluation-other"  # type: ignore[index]
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(payload))

    with pytest.raises(ReconciliationCollectionBlocked) as caught:
        CampaignSnapshotCollector(
            source=source,
            campaign_id="evaluation-studio-v1",
            tenant_id="tenant-evaluation",
        )(EvidenceStore(tmp_path / "bundle"))

    assert caught.value.code == "campaign_tag_mismatch"


def test_bounded_command_producers_capture_actual_exit_and_check_verdict(
    tmp_path: Path,
) -> None:
    playwright = BoundedPlaywrightProducer(
        artifact_root=tmp_path / "browser",
        command=(sys.executable, "-c", "print('playwright complete')"),
        working_directory=tmp_path,
        timeout_seconds=5,
    )()
    check = BoundedZerothCheckRunner(
        command=(
            sys.executable,
            "-c",
            "print('Zeroth Check: PASS (exit 0)')",
        ),
        working_directory=tmp_path,
        timeout_seconds=5,
    )()

    assert playwright.exit_code == 0 and "complete" in playwright.stdout
    assert check.exit_code == 0 and check.verdict == "pass"


def test_snapshot_collector_lazily_runs_and_persists_bounded_export_command(
    tmp_path: Path,
) -> None:
    campaign_id = "evaluation-studio-v1"
    tenant_id = "tenant-evaluation"
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_snapshot(campaign_id, tenant_id)))
    output = tmp_path / "produced.json"
    producer = BoundedSnapshotProducer(
        output_path=output,
        command=(
            sys.executable,
            "-c",
            "import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])",
            str(template),
            str(output),
        ),
        working_directory=tmp_path,
        timeout_seconds=5,
    )
    store = EvidenceStore(tmp_path / "bundle")
    collector = CampaignSnapshotCollector(
        source=output,
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        producer=producer,
    )

    collector(store)

    commands = tuple((store.root / "commands").glob("*.json"))
    assert len(commands) == 1
    assert json.loads(commands[0].read_text())["exit_code"] == 0
    assert (store.root / "reconciliation/input.json").is_file()


def test_snapshot_collector_preserves_exporters_stable_blocker_code(
    tmp_path: Path,
) -> None:
    output = tmp_path / "not-produced.json"
    producer = BoundedSnapshotProducer(
        output_path=output,
        command=(
            sys.executable,
            "-c",
            "import json;print(json.dumps({'status':'blocked','reason':'provider_window_missing'}));raise SystemExit(2)",
        ),
        working_directory=tmp_path,
        timeout_seconds=5,
    )
    store = EvidenceStore(tmp_path / "bundle")

    with pytest.raises(ReconciliationCollectionBlocked) as caught:
        CampaignSnapshotCollector(
            source=output,
            campaign_id="evaluation-studio-v1",
            tenant_id="tenant-evaluation",
            producer=producer,
        )(store)

    assert caught.value.code == "provider_window_missing"
    command = next((store.root / "commands").glob("*.json"))
    assert json.loads(command.read_text())["exit_code"] == 2

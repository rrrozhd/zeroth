from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.rightsizing_authoritative_reconciliation import (
    AuthoritativeRightsizingReconciliationCollector,
    RightsizingReconciliationBlocked,
)
from release.live_evaluation.rightsizing_service_adapter import (
    ServiceCallIdentity,
    ServiceExperimentIdentity,
)


CAMPAIGN = "campaign-rightsizing-1"
TENANT = "tenant-a"


class _Audits:
    def __init__(self, rows: tuple[dict[str, object], ...], *, signed: bool = True) -> None:
        self.rows = rows
        self.signed = signed
        self.verified_runs: list[str] = []

    def records_for_runs(self, run_ids):
        return tuple(row for row in self.rows if row["run_id"] in run_ids)

    def verify_run(self, run_id):
        self.verified_runs.append(run_id)
        return {
            "verified": self.signed,
            "signature_verified": self.signed,
            "unsigned_record_count": 0 if self.signed else 1,
            "run_id": run_id,
            "tenant_id": TENANT,
            "campaign_id": CAMPAIGN,
        }


def _audit(run_id: str, suffix: str) -> dict[str, object]:
    return {
        "audit_id": f"audit-{suffix}",
        "run_id": run_id,
        "tenant_id": TENANT,
        "status": "completed",
        "cost_usd": "0.001",
        "cost_event_id": f"cost-{suffix}",
        "record_signature": f"signature-{suffix}",
        "execution_metadata": {},
    }


def _databases(tmp_path: Path, rows: tuple[tuple[str, str], ...]) -> tuple[Path, Path, Path]:
    econ = tmp_path / "econ.sqlite3"
    with sqlite3.connect(econ) as db:
        db.execute(
            """CREATE TABLE cost_reservations (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, run_id TEXT,
            status TEXT, max_cost_usd TEXT, held_cost_usd TEXT, actual_cost_usd TEXT,
            cost_event_id TEXT, provider_request_id TEXT, cleanup_status TEXT)"""
        )
        db.execute(
            """CREATE TABLE execution_events (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT, execution_id TEXT,
            token_cost_usd TEXT, tool_cost_usd TEXT, compute_cost_usd TEXT, metadata TEXT)"""
        )
        db.execute(
            "CREATE TABLE outcome_events (tenant_id TEXT, execution_id TEXT, "
            "outcome_payload_json TEXT)"
        )
        for run_id, suffix in rows:
            db.execute(
                "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    TENANT,
                    CAMPAIGN,
                    f"operation-{suffix}",
                    run_id,
                    "committed",
                    "0.25",
                    "0",
                    "0.001",
                    f"cost-{suffix}",
                    f"provider-{suffix}",
                    "complete",
                ),
            )
            db.execute(
                "INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    TENANT,
                    CAMPAIGN,
                    f"operation-{suffix}",
                    f"provider-{suffix}",
                    "complete",
                    f"cost-{suffix}",
                    "0.001",
                    "0",
                    "0",
                    json.dumps({"run_id": run_id}),
                ),
            )
    sink = tmp_path / "actions.sqlite3"
    with sqlite3.connect(sink) as db:
        db.execute(
            "CREATE TABLE action_markers (operation_key TEXT, receipt TEXT, payload_hash TEXT)"
        )
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.01"}))
    return econ, sink, provider


def _identity(run_id: str = "rightsizing:run-1", suffix: str = "1") -> ServiceExperimentIdentity:
    return ServiceExperimentIdentity(
        campaign_id=CAMPAIGN,
        run_id=run_id,
        calls=(
            ServiceCallIdentity(
                operation_id=f"operation-{suffix}",
                provider_request_id=f"provider-{suffix}",
                cost_event_id=f"cost-{suffix}",
                audit_event_id=f"audit-{suffix}",
            ),
        ),
    )


def _collector(
    tmp_path: Path,
    *,
    rows: tuple[tuple[str, str], ...] = (("rightsizing:run-1", "1"),),
    signed: bool = True,
    run_inventory=None,
):
    econ, sink, provider = _databases(tmp_path, rows)
    audits = _Audits(tuple(_audit(run_id, suffix) for run_id, suffix in rows), signed=signed)
    collector = AuthoritativeRightsizingReconciliationCollector(
        tenant_id=TENANT,
        econ_database=econ,
        action_sink_database=sink,
        audit_source=audits,
        provider_window=provider,
        run_inventory=run_inventory,
    )
    return collector, audits


def test_collects_typed_exact_records_from_authoritative_persisted_planes(
    tmp_path: Path,
) -> None:
    collector, audits = _collector(tmp_path)

    result = collector.collect(_identity())

    assert [row.provider_request_id for row in result.audits] == ["provider-1"]
    assert [row.operation_id for row in result.reservations] == ["operation-1"]
    assert [row.cost_event_id for row in result.local_cost_events] == ["cost-1"]
    assert [row.execution_event_id for row in result.regulus_events] == ["cost-1"]
    assert result.action_receipts == ()
    assert result.provider_window.window_id == "window-1"
    assert result.provider_window.total_usd == Decimal("0.01")
    assert audits.verified_runs == ["rightsizing:run-1"]


def test_collects_when_provider_request_id_is_unavailable(tmp_path: Path) -> None:
    collector, _ = _collector(tmp_path)
    with sqlite3.connect(collector._econ_database) as database:
        database.execute("UPDATE cost_reservations SET provider_request_id = NULL")
        database.execute("UPDATE execution_events SET provider_request_id = NULL")
    identity = ServiceExperimentIdentity(
        campaign_id=CAMPAIGN,
        run_id="rightsizing:run-1",
        calls=(
            ServiceCallIdentity(
                operation_id="operation-1",
                provider_request_id=None,
                cost_event_id="cost-1",
                audit_event_id="audit-1",
            ),
        ),
    )

    result = collector.collect(identity)

    assert result.audits[0].provider_request_id is None
    assert result.local_cost_events[0].provider_request_id is None
    assert result.regulus_events[0].provider_request_id is None


def test_response_identity_must_exactly_match_every_persisted_plane(tmp_path: Path) -> None:
    collector, _ = _collector(tmp_path)
    wrong = ServiceExperimentIdentity(
        campaign_id=CAMPAIGN,
        run_id="rightsizing:run-1",
        calls=(
            ServiceCallIdentity(
                operation_id="operation-1",
                provider_request_id="provider-other",
                cost_event_id="cost-1",
                audit_event_id="audit-1",
            ),
        ),
    )

    with pytest.raises(RightsizingReconciliationBlocked) as caught:
        collector.collect(wrong)

    assert caught.value.code == "service_identity_mismatch"


def test_exporter_block_is_preserved_as_a_sanitized_reason(tmp_path: Path) -> None:
    collector, _ = _collector(tmp_path, signed=False)

    with pytest.raises(RightsizingReconciliationBlocked) as caught:
        collector.collect(_identity())

    assert caught.value.code == "signed_audit_verification_failed"


def test_campaign_inventory_verifies_all_runs_then_returns_only_target_run(
    tmp_path: Path,
) -> None:
    inventory_calls = []

    def inventory(campaign_id: str, tenant_id: str):
        inventory_calls.append((campaign_id, tenant_id))
        return ("rightsizing:run-1", "rightsizing:run-2")

    collector, audits = _collector(
        tmp_path,
        rows=(("rightsizing:run-1", "1"), ("rightsizing:run-2", "2")),
        run_inventory=inventory,
    )

    result = collector.collect(_identity(run_id="rightsizing:run-2", suffix="2"))

    assert inventory_calls == [(CAMPAIGN, TENANT)]
    assert audits.verified_runs == ["rightsizing:run-1", "rightsizing:run-2"]
    assert [row.run_id for row in result.audits] == ["rightsizing:run-2"]
    assert [row.operation_id for row in result.reservations] == ["operation-2"]


def test_incomplete_campaign_run_inventory_fails_before_export(tmp_path: Path) -> None:
    collector, audits = _collector(
        tmp_path,
        run_inventory=lambda _campaign, _tenant: ("rightsizing:other-run",),
    )

    with pytest.raises(RightsizingReconciliationBlocked) as caught:
        collector.collect(_identity())

    assert caught.value.code == "campaign_run_inventory_incomplete"
    assert audits.verified_runs == []


def test_extra_provider_call_in_target_run_is_rejected(tmp_path: Path) -> None:
    collector, _ = _collector(
        tmp_path,
        rows=(("rightsizing:run-1", "1"), ("rightsizing:run-1", "extra")),
    )

    with pytest.raises(RightsizingReconciliationBlocked) as caught:
        collector.collect(_identity())

    assert caught.value.code == "service_identity_mismatch"

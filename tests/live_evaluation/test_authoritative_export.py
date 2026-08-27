from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import httpx

from release.live_evaluation.reconciliation_export import (
    AuthoritativeCampaignExporter,
    AuthoritativeExportBlocked,
    HttpAuditSource,
    run_ids_from_events,
)


class Audits:
    def records_for_runs(self, run_ids):
        assert run_ids == ("run-1",)
        return (
            {
                "audit_id": "audit-1",
                "run_id": "run-1",
                "tenant_id": "evaluation-tenant",
                "status": "completed",
                "cost_usd": "0.01",
                "cost_event_id": "cost-1",
                "record_signature": "signature-1",
                "execution_metadata": {
                    # Provider/campaign identifiers deliberately do not survive
                    # the metadata-only audit capture boundary as raw text.
                },
            },
        )

    def verify_run(self, run_id):
        assert run_id == "run-1"
        return {
            "verified": True,
            "signature_verified": True,
            "unsigned_record_count": 0,
            "run_id": "run-1",
            "tenant_id": "evaluation-tenant",
            "campaign_id": "evaluation-studio-v1",
        }


def _databases(tmp_path: Path) -> tuple[Path, Path]:
    econ = tmp_path / "econ.sqlite3"
    with sqlite3.connect(econ) as db:
        db.execute(
            """CREATE TABLE cost_reservations (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, run_id TEXT,
            status TEXT, max_cost_usd TEXT, held_cost_usd TEXT, actual_cost_usd TEXT,
            cost_event_id TEXT, provider_request_id TEXT, cleanup_status TEXT)"""
        )
        db.execute(
            "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evaluation-tenant", "evaluation-studio-v1", "op-1", "run-1",
                "committed", "0.02", "0.01", "0.01", "cost-1", "provider-1",
                "committed",
            ),
        )
        db.execute(
            """CREATE TABLE execution_events (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT, execution_id TEXT, token_cost_usd TEXT,
            tool_cost_usd TEXT, compute_cost_usd TEXT, metadata TEXT)"""
        )
        db.execute(
            "INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "evaluation-tenant", "evaluation-studio-v1", "op-1", "provider-1",
                "committed", "cost-1", "0.01", "0", "0",
                json.dumps({"run_id": "run-1"}),
            ),
        )
        db.execute(
            "CREATE TABLE outcome_events (tenant_id TEXT, execution_id TEXT, "
            "outcome_payload_json TEXT)"
        )
    sink = tmp_path / "actions.sqlite3"
    with sqlite3.connect(sink) as db:
        db.execute(
            "CREATE TABLE action_markers (operation_key TEXT, receipt TEXT, payload_hash TEXT)"
        )
    return econ, sink


def test_authoritative_export_joins_only_exact_tagged_identities(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=Audits(),
        provider_window=provider,
    ).export()

    assert payload["audits"][0]["audit_event_id"] == "audit-1"
    assert payload["local_cost_events"][0]["cost_event_id"] == "cost-1"
    assert payload["regulus_events"][0]["execution_event_id"] == "cost-1"
    assert payload["regulus_events"][0]["valuation_recorded"] is False
    assert payload["regulus_events"][0]["failure_tax_usd"] == "0"
    assert payload["excluded_reservations"] == []


def test_authoritative_export_scopes_economics_to_requested_run_inventory(
    tmp_path: Path,
) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evaluation-tenant", "evaluation-studio-v1", "op-other", "run-other",
                "committed", "0.03", "0.02", "0.02", "cost-other", "provider-other",
                "committed",
            ),
        )
        db.execute(
            "INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "evaluation-tenant", "evaluation-studio-v1", "op-other", "provider-other",
                "committed", "cost-other", "0.02", "0", "0",
                json.dumps({"run_id": "run-other"}),
            ),
        )
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=Audits(),
        provider_window=provider,
    ).export()

    assert [row["run_id"] for row in payload["reservations"]] == ["run-1"]
    assert [row["run_id"] for row in payload["local_cost_events"]] == ["run-1"]


def test_authoritative_export_ignores_action_receipts_outside_requested_runs(
    tmp_path: Path,
) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(sink) as db:
        db.execute(
            "INSERT INTO action_markers VALUES (?,?,?)",
            ("operation-from-another-run", "receipt-other", "hash-other"),
        )
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=Audits(),
        provider_window=provider,
    ).export()

    assert payload["action_receipts"] == []


def test_authoritative_export_correlates_lifecycle_and_runtime_audit_projection(
    tmp_path: Path,
) -> None:
    class ProjectedAudits(Audits):
        def records_for_runs(self, run_ids):
            [runtime] = super().records_for_runs(run_ids)
            runtime = {
                **runtime,
                "cost_usd": None,
                "estimated_cost_usd": "0.01",
                "cost_measurement": "estimated",
            }
            lifecycle = {
                **runtime,
                "audit_id": "audit_cost-1",
                "cost_usd": "0.01",
                "estimated_cost_usd": None,
            }
            return (lifecycle, runtime)

    econ, sink = _databases(tmp_path)
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=ProjectedAudits(),
        provider_window=provider,
    ).export()

    assert [row["audit_event_id"] for row in payload["audits"]] == ["audit-1"]


def test_authoritative_export_proves_released_provider_not_called(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evaluation-tenant", "evaluation-studio-v1", "op-released", "run-1",
                "released", "0.02", "0", "0", None, None, "provider_not_called",
            ),
        )
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=Audits(),
        provider_window=provider,
    ).export()

    assert payload["excluded_reservations"] == [
        {
            "campaign_id": "evaluation-studio-v1",
            "tenant_id": "evaluation-tenant",
            "reservation_id": "reservation-2",
            "operation_id": "op-released",
            "run_id": "run-1",
            "reason": "provider_not_called",
            "cleanup_status": "provider_not_called",
        }
    ]


def test_authoritative_export_rejects_unproven_released_reservation(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute("UPDATE cost_reservations SET status='released', cleanup_status=NULL")
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=Audits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "released_reservation_outcome_unproven"


def test_authoritative_export_blocks_missing_regulus_join(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute("UPDATE execution_events SET operation_id = NULL")
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=Audits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "regulus_identity_join_incomplete"


def test_authoritative_export_blocks_cross_tenant_audit_identity(tmp_path: Path) -> None:
    class CrossTenantAudits(Audits):
        def records_for_runs(self, run_ids):
            [record] = super().records_for_runs(run_ids)
            return ({**record, "tenant_id": "other-tenant"},)

    econ, sink = _databases(tmp_path)
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=CrossTenantAudits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "audit_tenant_identity_mismatch"


def test_authoritative_export_blocks_cross_campaign_run_identity(tmp_path: Path) -> None:
    class CrossCampaignAudits(Audits):
        def verify_run(self, run_id):
            return {**super().verify_run(run_id), "campaign_id": "other-campaign"}

    econ, sink = _databases(tmp_path)
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=CrossCampaignAudits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "audit_campaign_identity_mismatch"


@pytest.mark.parametrize("mutation", ["cost_event", "duplicate"])
def test_authoritative_export_blocks_ambiguous_audit_identity(
    tmp_path: Path, mutation: str
) -> None:
    class BrokenAudits(Audits):
        def records_for_runs(self, run_ids):
            [record] = super().records_for_runs(run_ids)
            if mutation == "cost_event":
                return ({**record, "cost_event_id": "other-cost"},)
            return (record, {**record, "audit_id": "audit-2"})

    econ, sink = _databases(tmp_path)
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=BrokenAudits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "audit_identity_join_incomplete"


def test_authoritative_export_blocks_regulus_cleanup_mismatch(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute("UPDATE execution_events SET cleanup_status='pending_regulus_delivery'")
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=Audits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "regulus_identity_join_incomplete"


def test_authoritative_export_preserves_ambiguous_call_without_provider_id(
    tmp_path: Path,
) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "UPDATE cost_reservations SET status='ambiguous', held_cost_usd=max_cost_usd, "
            "actual_cost_usd='0', provider_request_id=NULL, cleanup_status='pending_reconciliation'"
        )
        db.execute(
            "UPDATE execution_events SET provider_request_id=NULL, "
            "cleanup_status='pending_reconciliation', token_cost_usd='0'"
        )
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0"}))

    payload = AuthoritativeCampaignExporter(
        econ_database=econ,
        action_sink_database=sink,
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-tenant",
        run_ids=("run-1",),
        audit_source=Audits(),
        provider_window=provider,
    ).export()

    assert payload["reservations"][0]["state"] == "held_ambiguous"
    assert payload["reservations"][0]["retained_usd"] == "0.02"
    assert payload["audits"][0]["provider_request_id"] is None
    assert payload["local_cost_events"][0]["provider_request_id"] is None
    assert payload["regulus_events"][0]["provider_request_id"] is None


def test_authoritative_export_blocks_one_sided_missing_provider_identity(
    tmp_path: Path,
) -> None:
    econ, sink = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "UPDATE cost_reservations SET status='ambiguous', held_cost_usd=max_cost_usd, "
            "provider_request_id=NULL, cleanup_status='pending_reconciliation'"
        )
        db.execute("UPDATE execution_events SET cleanup_status='pending_reconciliation'")
    provider = tmp_path / "provider-window.json"
    provider.write_text(json.dumps({"window_id": "window-1", "total_usd": "0.02"}))

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=Audits(),
            provider_window=provider,
        ).export()

    assert caught.value.code == "regulus_identity_join_incomplete"


def test_authoritative_export_names_missing_provider_window(tmp_path: Path) -> None:
    econ, sink = _databases(tmp_path)

    with pytest.raises(AuthoritativeExportBlocked) as caught:
        AuthoritativeCampaignExporter(
            econ_database=econ,
            action_sink_database=sink,
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-tenant",
            run_ids=("run-1",),
            audit_source=Audits(),
            provider_window=tmp_path / "missing-provider-window.json",
        ).export()

    assert caught.value.code == "provider_window_missing"


def test_http_audit_source_uses_public_records_and_signed_verification() -> None:
    requested: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "audit-1",
                            "run_id": "run-1",
                            "status": "completed",
                            "execution_metadata": {
                                "campaign_id": "evaluation-studio-v1"
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/v1/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "tenant_id": "evaluation-tenant",
                    "campaign_id": "evaluation-studio-v1",
                },
            )
        if request.url.path == "/v1/runs/run-1/audit-verification":
            return httpx.Response(
                200,
                json={
                    "verified": True,
                    "signature_verified": True,
                    "unsigned_record_count": 0,
                },
            )
        return httpx.Response(404)

    source = HttpAuditSource(
        deployments={"deployment-1": "http://127.0.0.1:8101"},
        headers={"X-API-Key": "local-test-key", "X-Tenant-ID": "evaluation-tenant"},
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    assert source.verify_run("run-1")["signature_verified"] is True
    assert source.verify_run("run-1")["campaign_id"] == "evaluation-studio-v1"
    assert source.records_for_runs(("run-1",))[0]["audit_id"] == "audit-1"
    assert ("GET", "/v1/runs/run-1/audit-verification") in requested


def test_run_inventory_comes_from_typed_campaign_event_correlations(tmp_path: Path) -> None:
    events = tmp_path / "events.ndjson"
    events.write_text(
        "\n".join(
            [
                json.dumps({"type": "campaign.api.completed", "correlation": {"run_id": "run-2"}}),
                json.dumps({"type": "campaign.audit.observed", "correlation": {"run_id": "run-1"}}),
                json.dumps({"type": "unrelated", "data": {"run_id": "must-not-use"}}),
            ]
        )
    )

    assert run_ids_from_events(events) == ("run-1", "run-2")

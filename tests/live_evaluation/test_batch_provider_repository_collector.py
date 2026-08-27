from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.batch_provider_repository_collector import (
    AuthoritativeBatchCollectionBlocked,
    RepositoryBackedBatchCollector,
)
from release.live_evaluation.batch_provider_service_adapter import BatchCollectionIdentity


class _Audits:
    def records_for_runs(self, run_ids: tuple[str, ...]):
        assert run_ids == tuple(f"child-{index}" for index in range(8))
        return tuple(
            {
                "audit_id": f"audit-{index}",
                "run_id": f"child-{index}",
                "tenant_id": "tenant-a",
                "campaign_id": "campaign-a",
                "status": "completed",
                "cost_usd": "0.001",
                "cost_event_id": f"cost-{index}",
                "record_signature": f"signature-{index}",
            }
            for index in range(8)
        )

    def verify_run(self, run_id: str):
        return {
            "verified": True,
            "signature_verified": True,
            "unsigned_record_count": 0,
            "run_id": run_id,
            "tenant_id": "tenant-a",
            "campaign_id": "campaign-a",
        }


def _history(index: int) -> str:
    origin = datetime(2026, 8, 26, 12, tzinfo=UTC)
    wave = index // 4
    started = origin + timedelta(seconds=wave * 2)
    completed = started + timedelta(seconds=1)
    return json.dumps(
        [
            {
                "node_id": f"branch:{index}:subgraph:provider-call",
                "audit_ref": f"audit-{index}",
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
            }
        ]
    )


def _databases(tmp_path: Path) -> tuple[Path, Path]:
    service = tmp_path / "service.sqlite3"
    with sqlite3.connect(service) as db:
        db.execute(
            """CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, tenant_id TEXT, parent_run_id TEXT,
            graph_version_ref TEXT, metadata TEXT, execution_history TEXT)"""
        )
        db.execute(
            """CREATE TABLE graph_versions (
            graph_id TEXT, version INTEGER, tenant_id TEXT, payload TEXT,
            PRIMARY KEY (graph_id, version))"""
        )
        graph = {
            "nodes": [
                {
                    "node_id": "batch",
                    "parallel_config": {"max_concurrency": 4},
                }
            ]
        }
        db.execute(
            "INSERT INTO graph_versions VALUES (?,?,?,?)",
            ("parent-graph", 1, "tenant-a", json.dumps(graph)),
        )
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?)",
            (
                "parent-1",
                "tenant-a",
                None,
                "parent-graph@1",
                json.dumps({"campaign_id": "campaign-a"}),
                "[]",
            ),
        )
        for index in range(8):
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (
                    f"child-{index}",
                    "tenant-a",
                    "parent-1",
                    "child-graph@1",
                    json.dumps({"campaign_id": "campaign-a"}),
                    _history(index),
                ),
            )

    econ = tmp_path / "econ.sqlite3"
    with sqlite3.connect(econ) as db:
        db.execute(
            """CREATE TABLE cost_reservations (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, run_id TEXT,
            status TEXT, max_cost_usd TEXT, held_cost_usd TEXT,
            actual_cost_usd TEXT, released_cost_usd TEXT, cost_event_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT)"""
        )
        db.execute(
            """CREATE TABLE execution_events (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT,
            provider_request_id TEXT, cleanup_status TEXT, execution_id TEXT,
            token_cost_usd TEXT, tool_cost_usd TEXT, compute_cost_usd TEXT,
            metadata TEXT)"""
        )
        for index in range(8):
            db.execute(
                "INSERT INTO cost_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "tenant-a",
                    "campaign-a",
                    f"operation-{index}",
                    f"child-{index}",
                    "committed",
                    "0.002",
                    "0.001",
                    "0.001",
                    "0.001",
                    f"cost-{index}",
                    f"provider-{index}",
                    "complete",
                ),
            )
            db.execute(
                "INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "tenant-a",
                    "campaign-a",
                    f"operation-{index}",
                    f"provider-{index}",
                    "complete",
                    f"cost-{index}",
                    "0.001",
                    "0",
                    "0",
                    json.dumps({"run_id": f"child-{index}"}),
                ),
            )
    return service, econ


def _identity() -> BatchCollectionIdentity:
    return BatchCollectionIdentity(
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        repetition=1,
        parent_run_id="parent-1",
        child_run_ids=tuple(f"child-{index}" for index in range(8)),
    )


def test_collects_exact_persisted_lineage_concurrency_and_economics(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=_Audits(),
    ).collect(_identity())

    assert result.parent_run_id == "parent-1"
    assert result.configured_concurrency == 4
    assert result.observed_peak_concurrency == 4
    assert result.campaign_spend_after_usd == Decimal("0.008")
    assert [child.item_index for child in result.children] == list(range(8))
    assert result.children[0].reservation_id == "operation-0"
    assert result.children[0].provider_request_id == "provider-0"
    assert result.children[0].regulus_execution_event_id == "cost-0"
    assert result.children[0].reservation_released_cost_usd == Decimal("0.001")


def test_collects_truthful_unavailable_provider_request_identity(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "UPDATE cost_reservations SET provider_request_id = NULL WHERE run_id = 'child-0'"
        )
        db.execute(
            "UPDATE execution_events SET provider_request_id = NULL "
            "WHERE operation_id = 'operation-0'"
        )

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=_Audits(),
    ).collect(_identity())

    assert result.children[0].provider_request_id is None


def test_missing_published_concurrency_is_an_explicit_blocker(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(service) as db:
        db.execute(
            "UPDATE graph_versions SET payload = ?",
            (json.dumps({"nodes": [{"node_id": "batch"}]}),),
        )

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "configured_concurrency_not_authoritative"


def test_missing_child_interval_is_an_explicit_blocker(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(service) as db:
        db.execute("UPDATE runs SET execution_history = '[]' WHERE run_id = 'child-0'")

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "observed_concurrency_not_authoritative"


def test_cross_tenant_or_unlinked_run_is_rejected_before_economics(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(service) as db:
        db.execute("UPDATE runs SET tenant_id = 'tenant-b' WHERE run_id = 'child-7'")

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "child_lineage_incomplete"


def test_duplicate_or_missing_plane_identity_fails_closed(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute("DELETE FROM execution_events WHERE operation_id = 'operation-3'")

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "regulus_identity_join_incomplete"


def test_unpriced_timeline_audits_do_not_create_false_duplicate_calls(tmp_path: Path) -> None:
    class TimelineAudits(_Audits):
        def records_for_runs(self, run_ids: tuple[str, ...]):
            return (
                *super().records_for_runs(run_ids),
                {
                    "audit_id": "audit-unpriced",
                    "run_id": "child-0",
                    "tenant_id": "tenant-a",
                    "campaign_id": "campaign-a",
                    "status": "completed",
                    "cost_usd": "0",
                    "cost_event_id": None,
                    "record_signature": "signature-unpriced",
                },
            )

    service, econ = _databases(tmp_path)

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=TimelineAudits(),
    ).collect(_identity())

    assert len(result.children) == 8


def test_signed_provider_probe_audit_does_not_duplicate_runtime_cost_event(
    tmp_path: Path,
) -> None:
    class InstrumentedAudits(_Audits):
        def records_for_runs(self, run_ids: tuple[str, ...]):
            records = super().records_for_runs(run_ids)
            return (
                *records,
                {
                    "audit_id": "audit_cost-0",
                    "run_id": "child-0",
                    "tenant_id": "tenant-a",
                    "campaign_id": "campaign-a",
                    "status": "completed",
                    "cost_usd": "0.001",
                    "cost_event_id": "cost-0",
                    "record_signature": "signature-probe",
                },
            )

    service, econ = _databases(tmp_path)

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=InstrumentedAudits(),
    ).collect(_identity())

    assert len(result.children) == 8


def test_subgraph_local_audit_ref_matches_persisted_namespaced_audit_id(
    tmp_path: Path,
) -> None:
    class NamespacedAudits(_Audits):
        def records_for_runs(self, run_ids: tuple[str, ...]):
            records = list(super().records_for_runs(run_ids))
            records[0] = {**records[0], "audit_id": "child-0:audit:2"}
            return tuple(records)

    service, econ = _databases(tmp_path)
    with sqlite3.connect(service) as db:
        history = json.loads(
            db.execute(
                "SELECT execution_history FROM runs WHERE run_id = 'child-0'"
            ).fetchone()[0]
        )
        history[0]["audit_ref"] = "audit:2"
        db.execute(
            "UPDATE runs SET execution_history = ? WHERE run_id = 'child-0'",
            (json.dumps(history),),
        )

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=NamespacedAudits(),
    ).collect(_identity())

    assert len(result.children) == 8


def test_absent_optional_regulus_cost_components_reconcile_as_zero(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute("UPDATE execution_events SET tool_cost_usd = NULL")
        db.execute("UPDATE execution_events SET compute_cost_usd = NULL")

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=_Audits(),
    ).collect(_identity())

    assert len(result.children) == 8


def test_estimated_runtime_audit_uses_estimated_cost_field(tmp_path: Path) -> None:
    class EstimatedAudits(_Audits):
        def records_for_runs(self, run_ids: tuple[str, ...]):
            return tuple(
                {
                    **record,
                    "cost_usd": None,
                    "estimated_cost_usd": "0.001",
                    "cost_measurement": "estimated",
                }
                for record in super().records_for_runs(run_ids)
            )

    service, econ = _databases(tmp_path)

    result = RepositoryBackedBatchCollector(
        service_database=service,
        econ_database=econ,
        audit_source=EstimatedAudits(),
    ).collect(_identity())

    assert len(result.children) == 8
    assert result.children[0].audit_cost_usd == Decimal("0.001")


def test_secret_shaped_provider_identity_is_rejected(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    secret_shape = "sk-proj-" + "x" * 24
    with sqlite3.connect(econ) as db:
        db.execute(
            "UPDATE cost_reservations SET provider_request_id = ? WHERE run_id = 'child-2'",
            (secret_shape,),
        )
        db.execute(
            "UPDATE execution_events SET provider_request_id = ? WHERE operation_id = 'operation-2'",
            (secret_shape,),
        )

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "reservation_identity_join_incomplete"


def test_cross_plane_cost_discrepancy_fails_in_collector(tmp_path: Path) -> None:
    service, econ = _databases(tmp_path)
    with sqlite3.connect(econ) as db:
        db.execute(
            "UPDATE execution_events SET token_cost_usd = '0.009' "
            "WHERE operation_id = 'operation-4'"
        )

    with pytest.raises(AuthoritativeBatchCollectionBlocked) as caught:
        RepositoryBackedBatchCollector(
            service_database=service,
            econ_database=econ,
            audit_source=_Audits(),
        ).collect(_identity())

    assert caught.value.code == "cross_plane_cost_reconciliation_failed"

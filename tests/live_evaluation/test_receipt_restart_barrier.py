from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from release.live_evaluation.action_runner import EVALUATION_ACTION_MANIFEST_SHA256
from release.live_evaluation.action_sink import EvaluationActionSink
from release.live_evaluation.receipt_restart_barrier import (
    ReceiptRestartBarrierStore,
    RestartBarrierAuditRepository,
)
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.runtime.runs import Run


def _record(*, run_id: str = "run-restart", operation_key: str = "operation-restart"):
    return NodeAuditRecord(
        audit_id=f"{run_id}:audit:3",
        run_id=run_id,
        node_id="apply-remediation",
        graph_version_ref="graph@5",
        deployment_ref="deployment-w3",
        tenant_id="evaluation-studio-v1",
        status="completed",
        execution_metadata={
            "manifest_ref_sha256": EVALUATION_ACTION_MANIFEST_SHA256,
            "operation_key": operation_key,
            "operation_state": "completed",
            "operation_first_execution": True,
            "operation_replay_suppressed": False,
        },
        record_digest="a" * 64,
        record_signature="hmac-sha256:" + "b" * 64,
    )


class _AuditDelegate:
    def __init__(self) -> None:
        self.records: list[NodeAuditRecord] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.records.append(record)
        return record

    def marker(self) -> str:
        return "delegated"


class _OperationStore:
    def __init__(self, receipt: dict[str, object] | None) -> None:
        self.receipt = receipt

    async def get(self, operation_key: str) -> dict[str, object] | None:
        if self.receipt is None:
            return None
        return {
            "operation_key": operation_key,
            "run_id": "run-restart",
            "state": "COMPLETED",
            "receipt": json.dumps(self.receipt, sort_keys=True),
        }


class _RunRepository:
    async def get(self, run_id: str) -> Run:
        return Run(
            run_id=run_id,
            graph_version_ref="graph@5",
            deployment_ref="deployment-w3",
            tenant_id="evaluation-studio-v1",
            current_node_ids=["apply-remediation"],
            audit_refs=["audit:1", "audit:2"],
        )


def _receipt(operation_key: str = "operation-restart") -> dict[str, object]:
    return {
        "operation_key": operation_key,
        "payload_hash": "c" * 64,
        "receipt": f"local-evaluation:{operation_key}:cccccccccccccccc",
        "created_at": "2026-08-24T12:00:00+00:00",
    }


@pytest.mark.anyio
async def test_barrier_is_persisted_after_signed_audit_and_before_run_checkpoint(
    tmp_path: Path,
) -> None:
    operation_key = "operation-restart"
    sink = EvaluationActionSink(tmp_path / "sink")
    marker = sink.execute(
        operation_key, {"ticket": "synthetic-1", "status": "remediated"}
    )
    receipt = {
        "operation_key": marker.operation_key,
        "payload_hash": marker.payload_hash,
        "receipt": marker.receipt,
        "created_at": marker.created_at,
    }
    store = ReceiptRestartBarrierStore(tmp_path / "barriers.sqlite3")
    delegate = _AuditDelegate()
    repository = RestartBarrierAuditRepository(
        delegate=delegate,
        barrier_store=store,
        fault_state=SimpleNamespace(
            consume=lambda **_kwargs: SimpleNamespace(mode="post_receipt_pre_checkpoint")
        ),
        operation_store=_OperationStore(receipt),
        run_repository=_RunRepository(),
        action_sink=sink,
        campaign_id="evaluation-studio-v1",
    )

    task = asyncio.create_task(repository.write(_record()))
    barrier = await asyncio.to_thread(
        store.wait_for,
        campaign_id="evaluation-studio-v1",
        run_id="run-restart",
        timeout_seconds=1,
    )

    assert len(delegate.records) == 1
    assert task.done() is False
    assert barrier["operation_key"] == operation_key
    assert barrier["audit_id"] == "run-restart:audit:3"
    assert barrier["audit_digest"] == "a" * 64
    assert barrier["audit_signature_sha256"] == hashlib.sha256(
        _record().record_signature.encode()
    ).hexdigest()
    assert barrier["operation_receipt_sha256"] == hashlib.sha256(
        json.dumps(receipt, sort_keys=True).encode()
    ).hexdigest()
    assert barrier["sink_payload_hash"] == marker.payload_hash
    assert barrier["pre_cut_audit_refs"] == ["audit:1", "audit:2"]

    repository.release("run-restart")
    assert (await asyncio.wait_for(task, timeout=1)).audit_id == "run-restart:audit:3"


@pytest.mark.anyio
async def test_non_action_and_unarmed_action_audits_never_block(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path / "sink")
    delegate = _AuditDelegate()
    repository = RestartBarrierAuditRepository(
        delegate=delegate,
        barrier_store=ReceiptRestartBarrierStore(tmp_path / "barriers.sqlite3"),
        fault_state=SimpleNamespace(consume=lambda **_kwargs: None),
        operation_store=_OperationStore(None),
        run_repository=_RunRepository(),
        action_sink=sink,
        campaign_id="evaluation-studio-v1",
    )
    ordinary = _record().model_copy(update={"execution_metadata": {}})

    assert await repository.write(ordinary) == ordinary
    assert (await repository.write(_record())).audit_id == "run-restart:audit:3"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"record_signature": None}, "signed audit"),
        ({"execution_metadata": {"manifest_ref_sha256": EVALUATION_ACTION_MANIFEST_SHA256}}, "operation identity"),
    ],
)
async def test_armed_barrier_fails_closed_for_incomplete_audit_identity(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    store = ReceiptRestartBarrierStore(tmp_path / "barriers.sqlite3")
    repository = RestartBarrierAuditRepository(
        delegate=_AuditDelegate(),
        barrier_store=store,
        fault_state=SimpleNamespace(
            consume=lambda **_kwargs: SimpleNamespace(mode="post_receipt_pre_checkpoint")
        ),
        operation_store=_OperationStore(_receipt()),
        run_repository=_RunRepository(),
        action_sink=EvaluationActionSink(tmp_path / "sink"),
        campaign_id="evaluation-studio-v1",
    )

    with pytest.raises(RuntimeError, match=message):
        await repository.write(_record().model_copy(update=change))


def test_barrier_store_is_single_use_per_run_and_operation(tmp_path: Path) -> None:
    store = ReceiptRestartBarrierStore(tmp_path / "barriers.sqlite3")
    values = {
        "campaign_id": "evaluation-studio-v1",
        "run_id": "run-restart",
        "deployment_ref": "deployment-w3",
        "operation_key": "operation-restart",
        "audit_id": "run-restart:audit:3",
        "audit_digest": "a" * 64,
        "audit_signature_sha256": "b" * 64,
        "operation_receipt_sha256": "c" * 64,
        "sink_receipt_sha256": "d" * 64,
        "sink_payload_hash": "e" * 64,
        "pre_cut_run_sha256": "f" * 64,
        "pre_cut_audit_refs": ["audit:1", "audit:2"],
    }

    store.capture(values)
    with pytest.raises(RuntimeError, match="already captured"):
        store.capture(values)


def test_default_hold_allows_native_ui_audit_proof_before_restart(tmp_path: Path) -> None:
    repository = RestartBarrierAuditRepository(
        delegate=_AuditDelegate(),
        barrier_store=ReceiptRestartBarrierStore(tmp_path / "barriers.sqlite3"),
        fault_state=SimpleNamespace(consume=lambda **_kwargs: None),
        operation_store=_OperationStore(None),
        run_repository=_RunRepository(),
        action_sink=EvaluationActionSink(tmp_path / "sink"),
        campaign_id="evaluation-studio-v1",
    )

    assert repository.wait_timeout_seconds == 120

"""Campaign-only cut point after a signed action audit and before run checkpointing.

The wrapper deliberately blocks only an explicitly armed synthetic-action audit.
The delegated audit write completes first; the orchestration recorder cannot append
the node history or write the next run checkpoint until this method returns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zeroth.governance.audit.models import NodeAuditRecord

from .action_runner import EVALUATION_ACTION_MANIFEST_SHA256
from .action_sink import EvaluationActionSink

_MODE = "post_receipt_pre_checkpoint"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ReceiptRestartBarrierStore:
    """Durable, sanitized observations consumed by the restart controller."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipt_restart_barriers (
                    campaign_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    deployment_ref TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    audit_digest TEXT NOT NULL,
                    audit_signature_sha256 TEXT NOT NULL,
                    operation_receipt_sha256 TEXT NOT NULL,
                    sink_receipt_sha256 TEXT NOT NULL,
                    sink_payload_hash TEXT NOT NULL,
                    pre_cut_run_sha256 TEXT NOT NULL,
                    pre_cut_audit_refs_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    restarted_at TEXT,
                    PRIMARY KEY (campaign_id, run_id),
                    UNIQUE (campaign_id, operation_key)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def capture(self, values: dict[str, str | list[str]]) -> dict[str, object]:
        required = {
            "campaign_id",
            "run_id",
            "deployment_ref",
            "operation_key",
            "audit_id",
            "audit_digest",
            "audit_signature_sha256",
            "operation_receipt_sha256",
            "sink_receipt_sha256",
            "sink_payload_hash",
            "pre_cut_run_sha256",
            "pre_cut_audit_refs",
        }
        if set(values) != required or not all(values[name] for name in required):
            raise RuntimeError("restart barrier evidence is incomplete")
        refs = values["pre_cut_audit_refs"]
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise RuntimeError("restart barrier pre-cut audit refs are malformed")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO receipt_restart_barriers (
                        campaign_id, run_id, deployment_ref, operation_key,
                        audit_id, audit_digest, audit_signature_sha256,
                        operation_receipt_sha256, sink_receipt_sha256,
                        sink_payload_hash, pre_cut_run_sha256,
                        pre_cut_audit_refs_json, state, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
                    """,
                    (
                        values["campaign_id"],
                        values["run_id"],
                        values["deployment_ref"],
                        values["operation_key"],
                        values["audit_id"],
                        values["audit_digest"],
                        values["audit_signature_sha256"],
                        values["operation_receipt_sha256"],
                        values["sink_receipt_sha256"],
                        values["sink_payload_hash"],
                        values["pre_cut_run_sha256"],
                        json.dumps(refs, separators=(",", ":")),
                        _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RuntimeError("restart barrier was already captured") from exc
            connection.commit()
        found = self.get(campaign_id=str(values["campaign_id"]), run_id=str(values["run_id"]))
        assert found is not None
        return found

    def get(self, *, campaign_id: str, run_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM receipt_restart_barriers
                WHERE campaign_id = ? AND run_id = ?
                """,
                (campaign_id, run_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["pre_cut_audit_refs"] = json.loads(result.pop("pre_cut_audit_refs_json"))
        return result

    def wait_for(
        self, *, campaign_id: str, run_id: str, timeout_seconds: float = 10.0
    ) -> dict[str, object]:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("restart barrier wait must be positive and bounded")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            record = self.get(campaign_id=campaign_id, run_id=run_id)
            if record is not None and record.get("state") == "waiting":
                return record
            time.sleep(0.05)
        raise RuntimeError("post-receipt pre-checkpoint barrier was not observed")

    def mark_restarted(self, *, campaign_id: str, run_id: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE receipt_restart_barriers
                SET state = 'restarted', restarted_at = ?
                WHERE campaign_id = ? AND run_id = ? AND state = 'waiting'
                """,
                (_utc_now(), campaign_id, run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("restart barrier could not be marked restarted")


class RestartBarrierAuditRepository:
    """Delegate audit storage, then hold one armed synthetic action in memory."""

    def __init__(
        self,
        *,
        delegate: Any,
        barrier_store: ReceiptRestartBarrierStore,
        fault_state: Any,
        operation_store: Any,
        run_repository: Any,
        action_sink: EvaluationActionSink,
        campaign_id: str,
        wait_timeout_seconds: float = 120.0,
    ) -> None:
        if not 0 < wait_timeout_seconds <= 180:
            raise ValueError("restart barrier hold must be positive and bounded")
        self.delegate = delegate
        self.barrier_store = barrier_store
        self.fault_state = fault_state
        self.operation_store = operation_store
        self.run_repository = run_repository
        self.action_sink = action_sink
        self.campaign_id = campaign_id
        self.wait_timeout_seconds = wait_timeout_seconds
        self._release_events: dict[str, asyncio.Event] = {}

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        stored = await self.delegate.write(record)
        metadata = record.execution_metadata
        if metadata.get("manifest_ref_sha256") != EVALUATION_ACTION_MANIFEST_SHA256:
            return stored
        fault = self.fault_state.consume(campaign_id=self.campaign_id, target="runtime")
        if fault is None:
            return stored
        if fault.mode != _MODE:
            raise RuntimeError("unsupported evaluation runtime barrier mode")
        if (
            metadata.get("operation_state") != "completed"
            or metadata.get("operation_first_execution") is not True
            or metadata.get("operation_replay_suppressed") is not False
        ):
            raise RuntimeError("restart barrier requires a first-execution operation identity")
        await self._capture_and_block(stored)
        return stored

    async def list_by_run(self, run_id: str, **kwargs: Any) -> list[NodeAuditRecord]:
        """Expose the delegate's durable tail to recovery-aware audit allocation."""
        return await self.delegate.list_by_run(run_id, **kwargs)

    async def _capture_and_block(self, record: NodeAuditRecord) -> None:
        if not record.record_digest or not record.record_signature:
            raise RuntimeError("restart barrier requires a durable signed audit")
        operation_key = record.execution_metadata.get("operation_key")
        if not isinstance(operation_key, str) or not operation_key:
            raise RuntimeError("restart barrier requires an operation identity")
        operation = await self.operation_store.get(operation_key)
        if (
            not isinstance(operation, dict)
            or operation.get("run_id") != record.run_id
            or str(operation.get("state", "")).upper() != "COMPLETED"
            or not isinstance(operation.get("receipt"), str)
        ):
            raise RuntimeError("restart barrier requires a completed operation receipt")
        try:
            operation_receipt = json.loads(str(operation["receipt"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("restart barrier operation receipt is malformed") from exc
        sink_receipt = self.action_sink.lookup(operation_key)
        if sink_receipt is None:
            raise RuntimeError("restart barrier requires an authoritative sink receipt")
        expected = {
            "operation_key": sink_receipt.operation_key,
            "payload_hash": sink_receipt.payload_hash,
            "receipt": sink_receipt.receipt,
            "created_at": sink_receipt.created_at,
        }
        if operation_receipt != expected:
            raise RuntimeError("operation and sink receipts do not reconcile")
        pre_cut = await self.run_repository.get(record.run_id)
        if pre_cut is None:
            raise RuntimeError("restart barrier requires a persisted pre-cut run")
        serialized_run = json.dumps(
            pre_cut.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        await asyncio.to_thread(
            self.barrier_store.capture,
            {
                "campaign_id": self.campaign_id,
                "run_id": record.run_id,
                "deployment_ref": record.deployment_ref,
                "operation_key": operation_key,
                "audit_id": record.audit_id,
                "audit_digest": record.record_digest,
                "audit_signature_sha256": _sha256(record.record_signature),
                "operation_receipt_sha256": _sha256(
                    json.dumps(operation_receipt, sort_keys=True)
                ),
                "sink_receipt_sha256": _sha256(sink_receipt.receipt),
                "sink_payload_hash": sink_receipt.payload_hash,
                "pre_cut_run_sha256": _sha256(serialized_run),
                "pre_cut_audit_refs": list(pre_cut.audit_refs),
            },
        )
        event = self._release_events.setdefault(record.run_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=self.wait_timeout_seconds)
        except TimeoutError as exc:
            raise RuntimeError("restart barrier expired before the owned restart") from exc

    def release(self, run_id: str) -> None:
        event = self._release_events.get(run_id)
        if event is None:
            raise RuntimeError("restart barrier is not waiting")
        event.set()

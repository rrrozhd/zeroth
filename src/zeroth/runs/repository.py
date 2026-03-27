"""SQLite-backed repositories for runs and threads.

This module handles all the database work for saving, loading, and updating
runs and threads.  It uses SQLite as the storage backend and manages schema
migrations automatically.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from governai import RunStatus

from zeroth.runs.models import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)
from zeroth.storage import Migration, SQLiteDatabase
from zeroth.storage.json import load_typed_value, to_json_value

SCHEMA_SCOPE = "run_threads"

MIGRATIONS = [
    Migration(
        version=1,
        name="create_run_thread_and_checkpoint_tables",
        sql="""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            checkpoint_id TEXT,
            parent_checkpoint_id TEXT,
            epoch INTEGER NOT NULL,
            workflow_name TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT,
            completed_steps TEXT NOT NULL,
            artifacts TEXT NOT NULL,
            channels TEXT NOT NULL,
            pending_approval TEXT,
            pending_interrupt_id TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            metadata TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            current_node_ids TEXT NOT NULL,
            pending_node_ids TEXT NOT NULL,
            execution_history TEXT NOT NULL,
            node_visit_counts TEXT NOT NULL,
            condition_results TEXT NOT NULL,
            audit_refs TEXT NOT NULL,
            final_output TEXT,
            failure_state TEXT
        );

        CREATE TABLE IF NOT EXISTS run_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_order INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            participating_agent_refs TEXT NOT NULL,
            state_snapshot_refs TEXT NOT NULL,
            checkpoint_refs TEXT NOT NULL,
            memory_bindings TEXT NOT NULL,
            run_ids TEXT NOT NULL,
            active_run_id TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="add_run_thread_scope_columns",
        sql="""
        ALTER TABLE runs
        ADD COLUMN tenant_id TEXT DEFAULT 'default';

        ALTER TABLE runs
        ADD COLUMN workspace_id TEXT;

        ALTER TABLE runs
        ADD COLUMN submitted_by TEXT;

        ALTER TABLE threads
        ADD COLUMN tenant_id TEXT DEFAULT 'default';

        ALTER TABLE threads
        ADD COLUMN workspace_id TEXT;

        UPDATE runs
        SET tenant_id = 'default'
        WHERE tenant_id IS NULL;

        UPDATE threads
        SET tenant_id = 'default'
        WHERE tenant_id IS NULL;

        CREATE INDEX IF NOT EXISTS idx_runs_scope
            ON runs(tenant_id, workspace_id, deployment_ref, thread_id, run_id);

        CREATE INDEX IF NOT EXISTS idx_threads_scope
            ON threads(tenant_id, workspace_id, deployment_ref, thread_id);
        """,
    ),
    Migration(
        version=3,
        name="add_durable_dispatch_columns",
        sql="""
        ALTER TABLE runs
        ADD COLUMN lease_worker_id TEXT;

        ALTER TABLE runs
        ADD COLUMN lease_acquired_at TEXT;

        ALTER TABLE runs
        ADD COLUMN lease_expires_at TEXT;

        ALTER TABLE runs
        ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE runs
        ADD COLUMN recovery_checkpoint_id TEXT;

        CREATE INDEX IF NOT EXISTS idx_runs_dispatch
            ON runs(deployment_ref, status, lease_expires_at);
        """,
    ),
    Migration(
        version=4,
        name="add_guardrail_tables",
        sql="""
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            bucket_key TEXT PRIMARY KEY,
            token_count REAL NOT NULL,
            last_refill_at TEXT NOT NULL,
            capacity REAL NOT NULL DEFAULT 10.0,
            refill_rate REAL NOT NULL DEFAULT 1.0
        );

        CREATE TABLE IF NOT EXISTS quota_counters (
            counter_key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            window_start TEXT NOT NULL,
            window_seconds INTEGER NOT NULL DEFAULT 86400
        );
        """,
    ),
]

ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_INTERRUPT,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_APPROVAL: {
        RunStatus.PENDING,  # durable worker re-queue after approval resolution
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_INTERRUPT: {
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: {RunStatus.PENDING},  # operator replay from dead-letter
}

# Sentinel stored in failure_state.reason to mark dead-letter runs.
DEAD_LETTER_REASON = "dead_letter"


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _new_checkpoint_id() -> str:
    """Generate a new random hex ID for a checkpoint."""
    return uuid4().hex


def _validate_transition(current: RunStatus, new: RunStatus) -> None:
    """Check that moving from one run status to another is valid.

    Raises ValueError if the transition is not allowed (for example,
    you can't go from COMPLETED back to RUNNING).
    """
    if new == current:
        return
    if new not in ALLOWED_TRANSITIONS[current]:
        msg = f"invalid run transition: {current.value} -> {new.value}"
        raise ValueError(msg)


def _dump_model(value: object | None) -> str | None:
    """Serialize a Pydantic model (or plain value) to a JSON string for storage."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return to_json_value(value)  # type: ignore[arg-type]
    return to_json_value(value)  # type: ignore[arg-type]


def _dump_list(items: Sequence[object]) -> str:
    """Serialize a list of Pydantic models to a JSON string for storage."""
    return to_json_value([item.model_dump(mode="json") for item in items])  # type: ignore[attr-defined]


def _merge(existing: list[str], updates: Sequence[str] | None) -> list[str]:
    """Merge new string items into an existing list, skipping duplicates."""
    merged = list(existing)
    for item in updates or []:
        if item not in merged:
            merged.append(item)
    return merged


def _merge_models(
    existing: list[ThreadMemoryBinding],
    updates: Sequence[ThreadMemoryBinding] | None,
) -> list[ThreadMemoryBinding]:
    """Merge new ThreadMemoryBinding items into an existing list, skipping duplicates."""
    merged = list(existing)
    for item in updates or []:
        if item not in merged:
            merged.append(item)
    return merged


@dataclass(slots=True)
class _RunThreadStore:
    """Low-level SQLite store that handles raw read/write operations for runs and threads.

    This is an internal class used by RunRepository and ThreadRepository.
    It owns the database connection and applies schema migrations on init.
    """

    database: SQLiteDatabase

    def __post_init__(self) -> None:
        self.database.apply_migrations(SCHEMA_SCOPE, MIGRATIONS)

    def save_run(self, run: Run) -> None:
        """Insert or update a run record in the database."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, checkpoint_id, parent_checkpoint_id, epoch, workflow_name,
                    status, current_step, completed_steps, artifacts, channels,
                    pending_approval, pending_interrupt_id, started_at, updated_at,
                    error, metadata, graph_version_ref, deployment_ref, tenant_id,
                    workspace_id, submitted_by, thread_id,
                    current_node_ids, pending_node_ids, execution_history,
                    node_visit_counts, condition_results, audit_refs, final_output,
                    failure_state
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(run_id) DO UPDATE SET
                    checkpoint_id = excluded.checkpoint_id,
                    parent_checkpoint_id = excluded.parent_checkpoint_id,
                    epoch = excluded.epoch,
                    workflow_name = excluded.workflow_name,
                    status = excluded.status,
                    current_step = excluded.current_step,
                    completed_steps = excluded.completed_steps,
                    artifacts = excluded.artifacts,
                    channels = excluded.channels,
                    pending_approval = excluded.pending_approval,
                    pending_interrupt_id = excluded.pending_interrupt_id,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    error = excluded.error,
                    metadata = excluded.metadata,
                    graph_version_ref = excluded.graph_version_ref,
                    deployment_ref = excluded.deployment_ref,
                    tenant_id = excluded.tenant_id,
                    workspace_id = excluded.workspace_id,
                    submitted_by = excluded.submitted_by,
                    thread_id = excluded.thread_id,
                    current_node_ids = excluded.current_node_ids,
                    pending_node_ids = excluded.pending_node_ids,
                    execution_history = excluded.execution_history,
                    node_visit_counts = excluded.node_visit_counts,
                    condition_results = excluded.condition_results,
                    audit_refs = excluded.audit_refs,
                    final_output = excluded.final_output,
                    failure_state = excluded.failure_state
                """,
                (
                    run.run_id,
                    run.checkpoint_id,
                    run.parent_checkpoint_id,
                    run.epoch,
                    run.workflow_name,
                    run.status.value,
                    run.current_step,
                    to_json_value(run.completed_steps),
                    to_json_value(run.artifacts),
                    to_json_value(run.channels),
                    _dump_model(run.pending_approval),
                    run.pending_interrupt_id,
                    run.started_at.isoformat(),
                    run.updated_at.isoformat(),
                    run.error,
                    to_json_value(run.metadata),
                    run.graph_version_ref,
                    run.deployment_ref,
                    run.tenant_id,
                    run.workspace_id,
                    _dump_model(run.submitted_by),
                    run.thread_id,
                    to_json_value(run.current_node_ids),
                    to_json_value(run.pending_node_ids),
                    _dump_list(run.execution_history),
                    to_json_value(run.node_visit_counts),
                    _dump_list(run.condition_results),
                    to_json_value(run.audit_refs),
                    _dump_model(run.final_output),
                    _dump_model(run.failure_state),
                ),
            )

    def save_thread(self, thread: Thread) -> None:
        """Insert or update a thread record in the database."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO threads (
                    thread_id, graph_version_ref, deployment_ref, tenant_id, workspace_id, status,
                    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                    memory_bindings, run_ids, active_run_id, last_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    graph_version_ref = excluded.graph_version_ref,
                    deployment_ref = excluded.deployment_ref,
                    tenant_id = excluded.tenant_id,
                    workspace_id = excluded.workspace_id,
                    status = excluded.status,
                    participating_agent_refs = excluded.participating_agent_refs,
                    state_snapshot_refs = excluded.state_snapshot_refs,
                    checkpoint_refs = excluded.checkpoint_refs,
                    memory_bindings = excluded.memory_bindings,
                    run_ids = excluded.run_ids,
                    active_run_id = excluded.active_run_id,
                    last_run_id = excluded.last_run_id,
                    updated_at = excluded.updated_at
                """,
                (
                    thread.thread_id,
                    thread.graph_version_ref,
                    thread.deployment_ref,
                    thread.tenant_id,
                    thread.workspace_id,
                    thread.status.value,
                    to_json_value(thread.participating_agent_refs),
                    to_json_value(thread.state_snapshot_refs),
                    to_json_value(thread.checkpoint_refs),
                    _dump_list(thread.memory_bindings),
                    to_json_value(thread.run_ids),
                    thread.active_run_id,
                    thread.last_run_id,
                    thread.created_at.isoformat(),
                    thread.updated_at.isoformat(),
                ),
            )

    def get_run(self, run_id: str) -> Run | None:
        """Load a run from the database by its ID, or return None if not found."""
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def get_thread(self, thread_id: str) -> Thread | None:
        """Load a thread from the database by its ID, or return None if not found."""
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_thread(row)

    def delete_run(self, run_id: str) -> None:
        """Remove a run from the database and update its parent thread."""
        run = self.get_run(run_id)
        if run is None:
            return
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        thread = self.get_thread(run.thread_id)
        if thread is None:
            return
        thread.run_ids = [current for current in thread.run_ids if current != run_id]
        if thread.active_run_id == run_id:
            thread.active_run_id = None
        if thread.last_run_id == run_id:
            thread.last_run_id = thread.run_ids[-1] if thread.run_ids else None
        thread.updated_at = _utc_now()
        self.save_thread(thread)

    def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run's current state as a checkpoint.

        Returns the checkpoint ID. Checkpoints let you restore a run
        to a previous point in its execution.
        """
        checkpoint_id = run.checkpoint_id or _new_checkpoint_id()
        run.checkpoint_id = checkpoint_id
        thread = self.get_thread(run.thread_id)
        if thread is None:
            self._record_thread_run(
                run.thread_id,
                run.run_id,
                run.graph_version_ref,
                run.deployment_ref,
                run.tenant_id,
                run.workspace_id,
            )
        run.touch()
        snapshot = run.model_dump(mode="json")
        checkpoint_order = self._next_checkpoint_order(run.thread_id)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                    checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    thread_id = excluded.thread_id,
                    checkpoint_order = excluded.checkpoint_order,
                    state_json = excluded.state_json
                """,
                (
                    checkpoint_id,
                    run.run_id,
                    run.thread_id,
                    checkpoint_order,
                    self._encode_checkpoint_json(to_json_value(snapshot)),
                    run.updated_at.isoformat(),
                ),
            )
        self._record_thread_checkpoint(run.thread_id, checkpoint_id)
        return checkpoint_id

    def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a previously saved checkpoint by its ID."""
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state_json FROM run_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return Run.model_validate(
            load_typed_value(self._decode_checkpoint_json(row["state_json"]), dict[str, Any])
        )

    def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a given thread."""
        checkpoint_ids = self._checkpoint_ids(thread_id)
        if not checkpoint_ids:
            return None
        return self.get_checkpoint(checkpoint_ids[-1])

    def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        checkpoints = (
            self.get_checkpoint(checkpoint_id) for checkpoint_id in self._checkpoint_ids(thread_id)
        )
        return [checkpoint for checkpoint in checkpoints if checkpoint is not None]

    def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread, or None."""
        thread = self.get_thread(thread_id)
        return None if thread is None else thread.active_run_id

    def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread, or None."""
        run_ids = self.list_run_ids(thread_id)
        return run_ids[-1] if run_ids else None

    def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        thread = self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.run_ids)

    def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        thread = self._ensure_thread(thread_id)
        thread.active_run_id = run_id
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.last_run_id = run_id
        thread.updated_at = _utc_now()
        self.save_thread(thread)

    def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread (only if it matches the given run_id)."""
        thread = self.get_thread(thread_id)
        if thread is None or thread.active_run_id != run_id:
            return
        thread.active_run_id = None
        thread.updated_at = _utc_now()
        self.save_thread(thread)

    def put_run(self, run: Run) -> None:
        """Save a run, creating its thread if needed, and write a checkpoint."""
        self._record_thread_run(
            run.thread_id,
            run.run_id,
            run.graph_version_ref,
            run.deployment_ref,
            run.tenant_id,
            run.workspace_id,
        )
        self.write_checkpoint(run)
        run.touch()
        self.save_run(run)

    def _ensure_thread(self, thread_id: str) -> Thread:
        """Load a thread by ID, raising KeyError if it doesn't exist."""
        thread = self.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return thread

    def _ensure_thread_from_run(self, run: Run) -> Thread:
        """Get or create a thread for a run, validating identity fields match."""
        thread = self.get_thread(run.thread_id)
        if thread is None:
            thread = Thread(
                thread_id=run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                status=ThreadStatus.ACTIVE,
                run_ids=[run.run_id],
                last_run_id=run.run_id,
            )
        else:
            if (
                thread.graph_version_ref != run.graph_version_ref
                or thread.deployment_ref != run.deployment_ref
                or thread.tenant_id != run.tenant_id
                or thread.workspace_id != run.workspace_id
            ):
                raise ValueError("thread identity mismatch")
            if run.run_id not in thread.run_ids:
                thread.run_ids.append(run.run_id)
            thread.last_run_id = run.run_id
            thread.updated_at = _utc_now()
        return thread

    def _record_thread_run(
        self,
        thread_id: str,
        run_id: str,
        graph_version_ref: str,
        deployment_ref: str,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        """Register a run with its thread, creating the thread if needed."""
        thread = self.get_thread(thread_id)
        if thread is None:
            thread = Thread(
                thread_id=thread_id,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                status=ThreadStatus.ACTIVE,
                run_ids=[run_id],
                last_run_id=run_id,
            )
        else:
            if (
                thread.graph_version_ref != graph_version_ref
                or thread.deployment_ref != deployment_ref
                or thread.tenant_id != tenant_id
                or thread.workspace_id != workspace_id
            ):
                raise ValueError("thread identity mismatch")
            thread.run_ids = _merge(thread.run_ids, [run_id])
            thread.last_run_id = run_id
            thread.updated_at = _utc_now()
        self.save_thread(thread)

    def _record_thread_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        """Add a checkpoint reference to a thread's list of checkpoints."""
        thread = self.get_thread(thread_id)
        if thread is None:
            return
        thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
        thread.updated_at = _utc_now()
        self.save_thread(thread)

    def _checkpoint_ids(self, thread_id: str) -> list[str]:
        """Return all checkpoint IDs for a thread."""
        thread = self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.checkpoint_refs)

    def _next_checkpoint_order(self, thread_id: str) -> int:
        """Return the next checkpoint order number for a thread."""
        return len(self._checkpoint_ids(thread_id))

    def _encode_checkpoint_json(self, payload: str) -> str:
        encrypted_field = self.database.encrypted_field
        if encrypted_field is None:
            return payload
        return encrypted_field.encrypt(payload)

    def _decode_checkpoint_json(self, payload: str) -> str:
        encrypted_field = self.database.encrypted_field
        if encrypted_field is None:
            return payload
        return encrypted_field.decrypt(payload)

    def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT checkpoint_id FROM run_checkpoints
                WHERE run_id = ?
                ORDER BY checkpoint_order DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return row["checkpoint_id"] if row else None

    def count_pending(self, deployment_ref: str) -> int:
        """Count runs with PENDING status for a deployment."""
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS cnt FROM runs WHERE status = ? AND deployment_ref = ?",
                (RunStatus.PENDING.value, deployment_ref),
            ).fetchone()
        return row["cnt"] if row else 0

    def increment_failure_count(self, run_id: str) -> int:
        """Atomically increment failure_count for a run; returns the new count."""
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET failure_count = failure_count + 1 WHERE run_id = ?",
                (run_id,),
            )
            row = connection.execute(
                "SELECT failure_count FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row["failure_count"] if row else 0

    def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status."""
        if status is not None:
            with self.database.transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE deployment_ref = ? AND status = ?
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (deployment_ref, status, limit, offset),
                ).fetchall()
        else:
            with self.database.transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE deployment_ref = ?
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (deployment_ref, limit, offset),
                ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return runs that have been dead-lettered (failed with dead_letter reason)."""
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE deployment_ref = ? AND status = ?
                ORDER BY updated_at DESC
                """,
                (deployment_ref, RunStatus.FAILED.value),
            ).fetchall()
        return [
            r
            for r in (self._row_to_run(row) for row in rows)
            if r.failure_state is not None and r.failure_state.reason == DEAD_LETTER_REASON
        ]

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        """Convert a raw SQLite row into a Run model."""
        return Run(
            run_id=row["run_id"],
            checkpoint_id=row["checkpoint_id"],
            parent_checkpoint_id=row["parent_checkpoint_id"],
            epoch=row["epoch"],
            workflow_name=row["workflow_name"],
            status=RunStatus(row["status"]),
            current_step=row["current_step"],
            completed_steps=load_typed_value(row["completed_steps"], list[str]) or [],
            artifacts=load_typed_value(row["artifacts"], dict[str, Any]) or {},
            channels=load_typed_value(row["channels"], dict[str, Any]) or {},
            pending_approval=load_typed_value(row["pending_approval"], Any),
            pending_interrupt_id=row["pending_interrupt_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            error=row["error"],
            metadata=load_typed_value(row["metadata"], dict[str, Any]) or {},
            graph_version_ref=row["graph_version_ref"],
            deployment_ref=row["deployment_ref"],
            tenant_id=row["tenant_id"] or "default",
            workspace_id=row["workspace_id"],
            submitted_by=load_typed_value(row["submitted_by"], dict[str, Any]),
            thread_id=row["thread_id"],
            current_node_ids=load_typed_value(row["current_node_ids"], list[str]) or [],
            pending_node_ids=load_typed_value(row["pending_node_ids"], list[str]) or [],
            execution_history=(
                load_typed_value(row["execution_history"], list[RunHistoryEntry]) or []
            ),
            node_visit_counts=load_typed_value(row["node_visit_counts"], dict[str, int]) or {},
            condition_results=(
                load_typed_value(row["condition_results"], list[RunConditionResult]) or []
            ),
            audit_refs=load_typed_value(row["audit_refs"], list[str]) or [],
            final_output=load_typed_value(row["final_output"], Any),
            failure_state=load_typed_value(row["failure_state"], dict[str, Any]),
        )

    def _row_to_thread(self, row: sqlite3.Row) -> Thread:
        """Convert a raw SQLite row into a Thread model."""
        return Thread(
            thread_id=row["thread_id"],
            graph_version_ref=row["graph_version_ref"],
            deployment_ref=row["deployment_ref"],
            tenant_id=row["tenant_id"] or "default",
            workspace_id=row["workspace_id"],
            status=ThreadStatus(row["status"]),
            participating_agent_refs=(
                load_typed_value(row["participating_agent_refs"], list[str]) or []
            ),
            state_snapshot_refs=load_typed_value(row["state_snapshot_refs"], list[str]) or [],
            checkpoint_refs=load_typed_value(row["checkpoint_refs"], list[str]) or [],
            memory_bindings=(
                load_typed_value(row["memory_bindings"], list[ThreadMemoryBinding]) or []
            ),
            run_ids=load_typed_value(row["run_ids"], list[str]) or [],
            active_run_id=row["active_run_id"],
            last_run_id=row["last_run_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class RunRepository:
    """High-level interface for saving and loading runs.

    Wraps the low-level store and adds business logic like status transitions,
    history recording, and checkpoint management.
    """

    def __init__(self, database: SQLiteDatabase):
        self._store = _RunThreadStore(database)

    def create(self, run: Run) -> Run:
        """Save a new run and return the persisted version."""
        self.put(run)
        return self.get(run.run_id)

    def put(self, run: Run) -> Run:
        """Save (insert or update) a run, including its checkpoint and thread."""
        self._store.put_run(run)
        return self.get(run.run_id)

    def get(self, run_id: str) -> Run | None:
        """Load a run by its ID, or return None if not found."""
        return self._store.get_run(run_id)

    def delete(self, run_id: str) -> None:
        """Remove a run from the database."""
        self._store.delete_run(run_id)

    def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run and return the checkpoint ID."""
        return self._store.write_checkpoint(run)

    def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a checkpoint by its ID."""
        return self._store.get_checkpoint(checkpoint_id)

    def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a thread."""
        return self._store.get_latest_checkpoint(thread_id)

    def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        return self._store.list_checkpoints(thread_id)

    def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread."""
        return self._store.get_active_run_id(thread_id)

    def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread."""
        return self._store.get_latest_run_id(thread_id)

    def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        return self._store.list_run_ids(thread_id)

    def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        self._store.set_active_run_id(thread_id, run_id)

    def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread if it matches the given run_id."""
        self._store.clear_active_run_id(thread_id, run_id)

    def transition(
        self,
        run_id: str,
        new_status: RunStatus,
        *,
        current_node_ids: Sequence[str] | None = None,
        pending_node_ids: Sequence[str] | None = None,
        current_step: str | None = None,
        completed_steps: Sequence[str] | None = None,
        final_output: object | None = None,
        failure_state: RunFailureState | None = None,
        audit_refs: Sequence[str] | None = None,
        error: str | None = None,
    ) -> Run:
        """Change a run's status, validating that the transition is allowed.

        Also lets you update node IDs, steps, output, and failure info
        in the same operation. Raises KeyError if the run doesn't exist,
        or ValueError if the status transition is invalid.
        """
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        _validate_transition(run.status, new_status)
        run.status = new_status
        if current_node_ids is not None:
            run.current_node_ids = list(current_node_ids)
            run.current_step = run.current_node_ids[0] if run.current_node_ids else None
        if pending_node_ids is not None:
            run.pending_node_ids = list(pending_node_ids)
        if current_step is not None:
            run.current_step = current_step
        if completed_steps is not None:
            run.completed_steps = list(completed_steps)
        if audit_refs is not None:
            run.audit_refs = list(audit_refs)
        if final_output is not None:
            run.final_output = final_output
        if failure_state is not None:
            run.failure_state = failure_state
            run.error = failure_state.message or failure_state.reason
        if error is not None:
            run.error = error
        run.touch()
        return self.put(run)

    def record_history(self, run_id: str, entry: RunHistoryEntry) -> Run:
        """Append a node execution entry to a run's history."""
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.execution_history.append(entry)
        run.completed_steps = [item.node_id for item in run.execution_history]
        run.touch()
        return self.put(run)

    def record_condition_result(self, run_id: str, result: RunConditionResult) -> Run:
        """Append a condition evaluation result to a run's records."""
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.condition_results.append(result)
        run.touch()
        return self.put(run)

    def count_pending(self, deployment_ref: str) -> int:
        """Count PENDING runs for a deployment (for backpressure checks)."""
        return self._store.count_pending(deployment_ref)

    def increment_failure_count(self, run_id: str) -> int:
        """Increment and return the failure_count for a run."""
        return self._store.increment_failure_count(run_id)

    def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the most recent checkpoint ID for a run."""
        return self._store.get_latest_checkpoint_id_for_run(run_id)

    def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status."""
        return self._store.list_runs(deployment_ref, status=status, limit=limit, offset=offset)

    def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return dead-lettered runs for a deployment."""
        return self._store.list_dead_letter_runs(deployment_ref)


class ThreadRepository:
    """High-level interface for saving and loading threads.

    Provides methods for creating, updating, and querying threads,
    as well as attaching runs and managing the active run.
    """

    def __init__(self, database: SQLiteDatabase):
        self._store = _RunThreadStore(database)

    def create(self, thread: Thread) -> Thread:
        """Save a new thread and return the persisted version."""
        self._store.save_thread(thread)
        return self.get(thread.thread_id)

    def get(self, thread_id: str) -> Thread | None:
        """Load a thread by its ID, or return None if not found."""
        return self._store.get_thread(thread_id)

    def list(self) -> list[Thread]:
        """Return all threads, ordered by creation time."""
        with self._store.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM threads ORDER BY created_at, thread_id"
            ).fetchall()
        return [self._store._row_to_thread(row) for row in rows]

    def update(self, thread: Thread) -> Thread:
        """Save changes to an existing thread."""
        thread.updated_at = _utc_now()
        self._store.save_thread(thread)
        return self.get(thread.thread_id)

    def resolve(
        self,
        thread_id: str | None,
        *,
        graph_version_ref: str,
        deployment_ref: str,
        tenant_id: str = "default",
        workspace_id: str | None = None,
        participating_agent_refs: Sequence[str] | None = None,
        state_snapshot_refs: Sequence[str] | None = None,
        checkpoint_refs: Sequence[str] | None = None,
        memory_bindings: Sequence[ThreadMemoryBinding] | None = None,
        run_id: str | None = None,
        status: ThreadStatus | None = None,
    ) -> Thread:
        """Find or create a thread, merging in any new data.

        If a thread with the given ID exists, its lists (agents, snapshots,
        checkpoints, etc.) are updated by merging in the new values.
        If it doesn't exist, a new thread is created with the provided data.
        """
        if thread_id is None:
            thread_id = run_id or uuid4().hex

        existing = self.get(thread_id)
        if existing is None:
            return self.create(
                Thread(
                    thread_id=thread_id,
                    graph_version_ref=graph_version_ref,
                    deployment_ref=deployment_ref,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    participating_agent_refs=list(participating_agent_refs or []),
                    state_snapshot_refs=list(state_snapshot_refs or []),
                    checkpoint_refs=list(checkpoint_refs or []),
                    memory_bindings=list(memory_bindings or []),
                    run_ids=[run_id] if run_id else [],
                    active_run_id=run_id,
                    last_run_id=run_id,
                    status=status or ThreadStatus.ACTIVE,
                )
            )

        if (
            existing.graph_version_ref != graph_version_ref
            or existing.deployment_ref != deployment_ref
            or existing.tenant_id != tenant_id
            or existing.workspace_id != workspace_id
        ):
            raise ValueError("thread identity mismatch")

        existing.participating_agent_refs = _merge(
            existing.participating_agent_refs,
            participating_agent_refs,
        )
        existing.state_snapshot_refs = _merge(existing.state_snapshot_refs, state_snapshot_refs)
        existing.checkpoint_refs = _merge(existing.checkpoint_refs, checkpoint_refs)
        existing.memory_bindings = _merge_models(existing.memory_bindings, memory_bindings)
        existing.run_ids = _merge(existing.run_ids, [run_id] if run_id else None)
        existing.active_run_id = run_id or existing.active_run_id
        existing.last_run_id = run_id or existing.last_run_id
        if status is not None:
            existing.status = status
        existing.updated_at = _utc_now()
        self._store.save_thread(existing)
        return self.get(thread_id)

    def attach_run(self, thread_id: str, run_id: str) -> Thread:
        """Add a run to a thread and make it the active run."""
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.active_run_id = run_id
        thread.last_run_id = run_id
        thread.updated_at = _utc_now()
        self._store.save_thread(thread)
        return self.get(thread_id)

    def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread."""
        return self._store.get_active_run_id(thread_id)

    def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread."""
        return self._store.get_latest_run_id(thread_id)

    def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        return self._store.list_run_ids(thread_id)

    def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        self._store.set_active_run_id(thread_id, run_id)

    def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread if it matches the given run_id."""
        self._store.clear_active_run_id(thread_id, run_id)

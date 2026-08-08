"""Concrete SQL persistence for runs.

The low-level store owns the raw ``runs`` and ``threads`` reads and writes and
the transaction boundaries around them; :class:`RunRepository` layers status
transitions, history recording, and checkpoint management on top.

The store deliberately spans both tables. Writing a checkpoint updates the
thread record, and registering a run creates its thread, so a run-only store
would have to reach across a boundary on nearly every write. The pieces that
*could* be separated without moving a transaction — row conversion, the
``run_checkpoints`` table, and the retention queries — live in their own
modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.integrations.persistence.runs import retention_queries
from zeroth.integrations.persistence.runs.checkpoint_store import (
    CheckpointRowStore,
)
from zeroth.integrations.persistence.runs.checkpoint_store import (
    new_checkpoint_id as _new_checkpoint_id,
)
from zeroth.integrations.persistence.runs.serialization import (
    dump_list as _dump_list,
)
from zeroth.integrations.persistence.runs.serialization import (
    dump_model as _dump_model,
)
from zeroth.integrations.persistence.runs.serialization import (
    row_to_run,
    row_to_thread,
)
from zeroth.integrations.persistence.runs.token_snapshot_store import TokenSnapshotRowStore
from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncConnection, AsyncDatabase
from zeroth.platform.storage.json import to_json_value
from zeroth.runtime.runs import (
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)

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
    """Low-level async store that handles raw read/write operations for runs and threads.

    This is an internal class used by RunRepository and ThreadRepository.
    It owns the database reference.
    """

    database: AsyncDatabase
    checkpoints: CheckpointRowStore = field(init=False)
    # ZER-26/AUD-004: per-run write fences. While a fence is installed for a
    # run id, every save of that run's row carries the lease predicate, so a
    # displaced worker's write is refused *inside* the statement rather than by
    # a check that races it.
    _fences: dict[str, tuple[str, int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Bind the ``run_checkpoints`` adapter to the same database."""
        self.checkpoints = CheckpointRowStore(self.database)

    def install_fence(self, run_id: str, worker_id: str, generation: int) -> None:
        """Fence every subsequent save of this run on (worker_id, generation)."""
        self._fences[run_id] = (worker_id, generation)

    def clear_fence(self, run_id: str) -> None:
        """Remove the write fence for a run, restoring unfenced saves."""
        self._fences.pop(run_id, None)

    async def save_run(self, run: Run) -> None:
        """Insert or update a run record in the database.

        When a fence is installed for this run, the upsert's UPDATE arm carries
        ``WHERE lease_worker_id = ? AND lease_generation = ?`` and the statement
        returns the written row — no row back means ownership moved and the
        write was refused, which raises :class:`FencedRunWriteRejectedError`. A
        fresh insert cannot conflict with a displaced owner, so the fence only
        gates the update arm.
        """
        fence = self._fences.get(run.run_id)
        async with self.database.transaction() as connection:
            await self._save_run_in_connection(connection, run, fence)

    async def _save_run_in_connection(
        self,
        connection: AsyncConnection,
        run: Run,
        fence: tuple[str, int] | None,
    ) -> None:
        fence_predicate = ""
        fence_params: tuple[object, ...] = ()
        if fence is not None:
            fence_predicate = "WHERE runs.lease_worker_id = ? AND runs.lease_generation = ?"
            fence_params = fence
        row = await connection.fetch_one(
            f"""
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
                {fence_predicate}
                RETURNING run_id
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
            )
            + fence_params,
        )
        if fence is not None and row is None:
            worker_id, generation = fence
            raise FencedRunWriteRejectedError(run.run_id, worker_id, generation)

    async def save_thread(self, thread: Thread) -> None:
        """Insert or update a thread record in the database."""
        async with self.database.transaction() as connection:
            await connection.execute(
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

    async def get_run(self, run_id: str, *, tenant_id: str | None = None) -> Run | None:
        """Load a run from the database by its ID, or return None if not found.

        WS-B: when ``tenant_id`` is supplied, a run owned by another tenant is
        invisible (returns ``None``). ``None`` = no tenant filter, the default
        for internal orchestrator lookups; API read paths pass the principal's
        tenant as defense-in-depth atop their existing scope checks.
        """
        sql = "SELECT * FROM runs WHERE run_id = ?"
        params: tuple[object, ...] = (run_id,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (run_id, tenant_id)
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(sql, params)
        if row is None:
            return None
        return row_to_run(row)

    async def get_thread(self, thread_id: str) -> Thread | None:
        """Load a thread from the database by its ID, or return None if not found."""
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            )
        if row is None:
            return None
        return row_to_thread(row)

    async def delete_run(self, run_id: str) -> None:
        """Remove a run from the database and update its parent thread."""
        run = await self.get_run(run_id)
        if run is None:
            return
        async with self.database.transaction() as connection:
            await connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        thread = await self.get_thread(run.thread_id)
        if thread is None:
            return
        thread.run_ids = [current for current in thread.run_ids if current != run_id]
        if thread.active_run_id == run_id:
            thread.active_run_id = None
        if thread.last_run_id == run_id:
            thread.last_run_id = thread.run_ids[-1] if thread.run_ids else None
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run's current state as a checkpoint.

        Returns the checkpoint ID. Checkpoints let you restore a run
        to a previous point in its execution. When the underlying database
        was configured with an encryption_key, the serialized state_json is
        encrypted at rest so thread-state checkpoints cannot leak secrets.
        """
        checkpoint_id = run.checkpoint_id or _new_checkpoint_id()
        run.checkpoint_id = checkpoint_id
        thread = await self.get_thread(run.thread_id)
        if thread is None:
            await self._record_thread_run(
                run.thread_id,
                run.run_id,
                run.graph_version_ref,
                run.deployment_ref,
                run.tenant_id,
                run.workspace_id,
            )
        run.touch()
        snapshot = run.model_dump(mode="json")
        checkpoint_order = await self._next_checkpoint_order(run.thread_id)
        await self.checkpoints.write_row(
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            thread_id=run.thread_id,
            checkpoint_order=checkpoint_order,
            state_json=to_json_value(snapshot),
            created_at=run.updated_at.isoformat(),
        )
        await self._record_thread_checkpoint(run.thread_id, checkpoint_id)
        return checkpoint_id

    async def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a previously saved checkpoint by its ID."""
        return await self.checkpoints.get(checkpoint_id)

    async def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a given thread."""
        checkpoint_ids = await self._checkpoint_ids(thread_id)
        if not checkpoint_ids:
            return None
        return await self.get_checkpoint(checkpoint_ids[-1])

    async def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        results: list[Run] = []
        for checkpoint_id in await self._checkpoint_ids(thread_id):
            checkpoint = await self.get_checkpoint(checkpoint_id)
            if checkpoint is not None:
                results.append(checkpoint)
        return results

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread, or None."""
        thread = await self.get_thread(thread_id)
        return None if thread is None else thread.active_run_id

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread, or None."""
        run_ids = await self.list_run_ids(thread_id)
        return run_ids[-1] if run_ids else None

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.run_ids)

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        thread = await self._ensure_thread(thread_id)
        thread.active_run_id = run_id
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.last_run_id = run_id
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread (only if it matches the given run_id)."""
        thread = await self.get_thread(thread_id)
        if thread is None or thread.active_run_id != run_id:
            return
        thread.active_run_id = None
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def put_run(self, run: Run) -> None:
        """Save a run, creating its thread if needed, and write a checkpoint."""
        await self._record_thread_run(
            run.thread_id,
            run.run_id,
            run.graph_version_ref,
            run.deployment_ref,
            run.tenant_id,
            run.workspace_id,
        )
        await self.write_checkpoint(run)
        run.touch()
        await self.save_run(run)

    async def _ensure_thread(self, thread_id: str) -> Thread:
        """Load a thread by ID, raising KeyError if it doesn't exist."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return thread

    async def _record_thread_run(
        self,
        thread_id: str,
        run_id: str,
        graph_version_ref: str,
        deployment_ref: str,
        tenant_id: str,
        workspace_id: str | None,
    ) -> None:
        """Register a run with its thread, creating the thread if needed."""
        thread = await self.get_thread(thread_id)
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
            if thread.tenant_id != tenant_id or thread.workspace_id != workspace_id:
                raise ValueError("thread identity mismatch")
            thread.run_ids = _merge(thread.run_ids, [run_id])
            thread.last_run_id = run_id
            thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def _record_thread_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        """Add a checkpoint reference to a thread's list of checkpoints."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            return
        thread.checkpoint_refs = _merge(thread.checkpoint_refs, [checkpoint_id])
        thread.updated_at = utc_now()
        await self.save_thread(thread)

    async def _checkpoint_ids(self, thread_id: str) -> list[str]:
        """Return all checkpoint IDs for a thread."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            return []
        return list(thread.checkpoint_refs)

    async def _next_checkpoint_order(self, thread_id: str) -> int:
        """Return the next checkpoint order number for a thread."""
        return len(await self._checkpoint_ids(thread_id))

    async def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the checkpoint_id for the most recent checkpoint of a run."""
        return await self.checkpoints.latest_id_for_run(run_id)

    async def count_pending(self, deployment_ref: str) -> int:
        """Count runs with PENDING status for a deployment."""
        async with self.database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT COUNT(*) AS cnt FROM runs WHERE status = ? AND deployment_ref = ?",
                (RunStatus.PENDING.value, deployment_ref),
            )
        return row["cnt"] if row else 0

    async def increment_failure_count(self, run_id: str) -> int:
        """Atomically increment failure_count for a run; returns the new count."""
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE runs SET failure_count = failure_count + 1 WHERE run_id = ?",
                (run_id,),
            )
            row = await connection.fetch_one(
                "SELECT failure_count FROM runs WHERE run_id = ?",
                (run_id,),
            )
        return row["failure_count"] if row else 0

    async def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status."""
        if status is not None:
            async with self.database.transaction() as connection:
                rows = await connection.fetch_all(
                    """
                    SELECT * FROM runs
                    WHERE deployment_ref = ? AND status = ?
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (deployment_ref, status, limit, offset),
                )
        else:
            async with self.database.transaction() as connection:
                rows = await connection.fetch_all(
                    """
                    SELECT * FROM runs
                    WHERE deployment_ref = ?
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (deployment_ref, limit, offset),
                )
        return [row_to_run(row) for row in rows]

    async def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return runs that have been dead-lettered (failed with dead_letter reason)."""
        async with self.database.transaction() as connection:
            rows = await connection.fetch_all(
                """
                SELECT * FROM runs
                WHERE deployment_ref = ? AND status = ?
                ORDER BY updated_at DESC
                """,
                (deployment_ref, RunStatus.FAILED.value),
            )
        return [
            r
            for r in (row_to_run(row) for row in rows)
            if r.failure_state is not None and r.failure_state.reason == DEAD_LETTER_REASON
        ]


class RunRepository:
    """High-level async interface for saving and loading runs.

    Wraps the low-level store and adds business logic like status transitions,
    history recording, and checkpoint management.
    """

    def __init__(self, database: AsyncDatabase):
        self._store = _RunThreadStore(database)
        self._token_snapshots = TokenSnapshotRowStore(database)

    @property
    def database(self) -> AsyncDatabase:
        """Database backing this repository, for coordinated service transactions."""
        return self._store.database

    async def create(self, run: Run) -> Run:
        """Save a new run and return the persisted version."""
        return await self.put(run)

    async def put(self, run: Run) -> Run:
        """Save (insert or update) a run, including its checkpoint and thread."""
        await self._store.put_run(run)
        return await self.get(run.run_id)

    def install_fence(self, run_id: str, worker_id: str, generation: int) -> None:
        """ZER-26/AUD-004: fence this run's saves on (worker_id, generation).

        While installed, every save of the run's row — the worker's own status
        transitions and the orchestrator's drive-time writes alike, since both
        share this repository — is refused in-statement once lease ownership
        moves, raising :class:`FencedRunWriteRejectedError`.
        """
        self._store.install_fence(run_id, worker_id, generation)

    def clear_fence(self, run_id: str) -> None:
        """Remove the write fence installed for a run."""
        self._store.clear_fence(run_id)

    async def get(self, run_id: str, *, tenant_id: str | None = None) -> Run | None:
        """Load a run by its ID, or return None if not found.

        WS-B: optional ``tenant_id`` filter (defense-in-depth). Default ``None``
        preserves the no-filter behaviour internal callers rely on.
        """
        return await self._store.get_run(run_id, tenant_id=tenant_id)

    async def delete(self, run_id: str) -> None:
        """Remove a run from the database."""
        await self._store.delete_run(run_id)

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        """Load the exact durable token-engine state for a run."""
        return await self._token_snapshots.get(run_id)

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        """Publish one coherent next token-engine revision atomically."""
        return await self._token_snapshots.compare_and_swap(
            run_id,
            expected_revision=expected_revision,
            snapshot=snapshot,
        )

    async def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run and return the checkpoint ID."""
        return await self._store.write_checkpoint(run)

    async def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a checkpoint by its ID."""
        return await self._store.get_checkpoint(checkpoint_id)

    async def get_latest_checkpoint(self, thread_id: str) -> Run | None:
        """Load the most recent checkpoint for a thread."""
        return await self._store.get_latest_checkpoint(thread_id)

    async def list_checkpoints(self, thread_id: str) -> list[Run]:
        """Return all checkpoints for a thread, in order."""
        return await self._store.list_checkpoints(thread_id)

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the currently active run ID for a thread."""
        return await self._store.get_active_run_id(thread_id)

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the most recently added run ID for a thread."""
        return await self._store.get_latest_run_id(thread_id)

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """Return all run IDs belonging to a thread."""
        return await self._store.list_run_ids(thread_id)

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        await self._store.set_active_run_id(thread_id, run_id)

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active run for a thread if it matches the given run_id."""
        await self._store.clear_active_run_id(thread_id, run_id)

    async def transition(
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
        run = await self.get(run_id)
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
        return await self.put(run)

    async def record_history(self, run_id: str, entry: RunHistoryEntry) -> Run:
        """Append a node execution entry to a run's history."""
        run = await self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.execution_history.append(entry)
        run.completed_steps = [item.node_id for item in run.execution_history]
        run.touch()
        return await self.put(run)

    async def record_condition_result(self, run_id: str, result: RunConditionResult) -> Run:
        """Append a condition evaluation result to a run's records."""
        run = await self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.condition_results.append(result)
        run.touch()
        return await self.put(run)

    async def count_pending(self, deployment_ref: str) -> int:
        """Count PENDING runs for a deployment (for backpressure checks)."""
        return await self._store.count_pending(deployment_ref)

    async def increment_failure_count(self, run_id: str) -> int:
        """Increment and return the failure_count for a run."""
        return await self._store.increment_failure_count(run_id)

    async def get_latest_checkpoint_id_for_run(self, run_id: str) -> str | None:
        """Return the most recent checkpoint ID for a run."""
        return await self._store.get_latest_checkpoint_id_for_run(run_id)

    async def list_runs(
        self,
        deployment_ref: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """Return runs for a deployment, optionally filtered by status."""
        return await self._store.list_runs(
            deployment_ref, status=status, limit=limit, offset=offset
        )

    async def list_dead_letter_runs(self, deployment_ref: str) -> list[Run]:
        """Return dead-lettered runs for a deployment."""
        return await self._store.list_dead_letter_runs(deployment_ref)

    async def erase_checkpoints_for_run(self, run_id: str) -> int:
        """WS-E: delete a run's checkpoints — the missing retention cascade.

        ``run_checkpoints.state_json`` holds the full serialized run state (the
        richest plaintext PII surface), and neither ``delete_run`` nor
        ``redact_run`` reaches it. Right-to-erasure / TTL purge calls this to
        remove that snapshot. Returns the number of checkpoint rows deleted;
        idempotent (a second call deletes nothing and returns 0).
        """
        async with self.database.transaction() as connection:
            return await self.erase_checkpoints_for_run_in_transaction(connection, run_id)

    async def erase_checkpoints_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Delete checkpoints through an existing transaction."""
        return await retention_queries.erase_checkpoints_for_run(connection, run_id)

    async def erase_token_snapshot_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Delete token-engine state through an existing erasure transaction."""
        return await retention_queries.erase_token_snapshot_for_run(connection, run_id)

    async def fence_token_snapshot_writes_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> bool:
        """Lock the run row and fence token writes during erasure."""
        return await retention_queries.fence_token_snapshot_writes(connection, run_id)

    async def fence_and_erase_token_snapshot_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> int:
        """Fence future writes and delete token state through one transaction."""
        return await retention_queries.fence_and_erase_token_snapshot_for_run(
            connection,
            run_id,
        )

    async def redact_run(self, run_id: str) -> bool:
        """WS-E: null a run's PII-bearing output columns, keeping the row.

        Nulls ``final_output``/``artifacts``/``metadata``/``error`` in place so
        the run row survives for chain/thread continuity while its plaintext
        payloads are gone. ``artifacts``/``metadata`` are NOT NULL columns, so
        they are reset to the empty-object sentinel rather than NULL. Idempotent.
        Returns True if the run existed.
        """
        async with self.database.transaction() as connection:
            return await self.redact_run_in_transaction(connection, run_id)

    async def redact_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> bool:
        """Redact a run through an existing transaction."""
        return await retention_queries.redact_run(connection, run_id)

    async def erasure_payloads_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> list[Any]:
        """Load database-resident run/checkpoint/token payloads before erasure."""
        return await retention_queries.erasure_payloads(
            connection,
            run_id,
            decrypt=self._store.checkpoints.decrypt_state_json,
        )

    async def tenant_id_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> str | None:
        """Resolve a run's persisted tenant inside a caller transaction."""
        return await retention_queries.tenant_id_for_run(connection, run_id)

    async def list_erasable_run_ids(
        self,
        tenant_id: str,
        older_than: datetime,
        *,
        terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
    ) -> list[str]:
        """Select TTL-erasable run ids: terminal status AND stale ``updated_at``.

        Only COMPLETED/FAILED runs qualify by default — PENDING, RUNNING, and
        the WAITING_* states are live work regardless of age. Selection is an
        unlocked snapshot; the destructive path must re-check via
        :meth:`lock_and_recheck_erasable_run` inside its own transaction.
        """
        async with self._store.database.transaction() as connection:
            return await retention_queries.select_erasable_run_ids(
                connection,
                tenant_id,
                older_than,
                terminal_statuses=terminal_statuses,
            )

    async def lock_and_recheck_erasable_run(
        self,
        connection: AsyncConnection,
        run_id: str,
        tenant_id: str,
        cutoff: datetime,
        *,
        terminal_statuses: frozenset[RunStatus] | set[RunStatus] | None = None,
    ) -> str | None:
        """Lock the run row and re-verify TTL eligibility before destruction.

        On PostgreSQL the row is locked with ``FOR UPDATE``; on SQLite the
        caller's write transaction already serializes writers. Returns ``None``
        when a replay/resume/update between selection and erasure made the run
        ineligible (wrong tenant, non-terminal status, or fresh ``updated_at``).
        """
        return await retention_queries.lock_and_recheck_erasable_run(
            connection,
            run_id,
            tenant_id,
            cutoff,
            terminal_statuses=terminal_statuses,
            lock=self._store.database.backend == "postgres",
        )

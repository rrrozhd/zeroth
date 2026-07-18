"""Async database-backed repositories for runs and threads.

The run persistence implementation now lives in
:mod:`zeroth.integrations.persistence.runs`. This module stays as the
protected legacy import location and re-exports it, so
``from zeroth.core.runs.repository import RunRepository`` keeps working.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from zeroth.core.runs.models import Thread, ThreadMemoryBinding, ThreadStatus
from zeroth.core.storage import AsyncDatabase
from zeroth.integrations.persistence.runs.run_repository import (
    ALLOWED_TRANSITIONS,
    DEAD_LETTER_REASON,
    RunRepository,
    _merge,
    _merge_models,
    _RunThreadStore,
)
from zeroth.integrations.persistence.runs.serialization import row_to_thread
from zeroth.platform.primitives import utc_now

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEAD_LETTER_REASON",
    "RunRepository",
    "ThreadRepository",
]


class ThreadRepository:
    """High-level async interface for saving and loading threads.

    Provides methods for creating, updating, and querying threads,
    as well as attaching runs and managing the active run.
    """

    def __init__(self, database: AsyncDatabase):
        self._store = _RunThreadStore(database)

    async def create(self, thread: Thread) -> Thread:
        """Save a new thread and return the persisted version."""
        await self._store.save_thread(thread)
        return await self.get(thread.thread_id)

    async def get(self, thread_id: str) -> Thread | None:
        """Load a thread by its ID, or return None if not found."""
        return await self._store.get_thread(thread_id)

    async def list(self) -> list[Thread]:
        """Return all threads, ordered by creation time."""
        async with self._store.database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM threads ORDER BY created_at, thread_id"
            )
        return [row_to_thread(row) for row in rows]

    async def update(self, thread: Thread) -> Thread:
        """Save changes to an existing thread."""
        thread.updated_at = utc_now()
        await self._store.save_thread(thread)
        return await self.get(thread.thread_id)

    async def resolve(
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

        existing = await self.get(thread_id)
        if existing is None:
            return await self.create(
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
        existing.updated_at = utc_now()
        await self._store.save_thread(existing)
        return await self.get(thread_id)

    async def attach_run(self, thread_id: str, run_id: str) -> Thread:
        """Add a run to a thread and make it the active run."""
        thread = await self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.active_run_id = run_id
        thread.last_run_id = run_id
        thread.updated_at = utc_now()
        await self._store.save_thread(thread)
        return await self.get(thread_id)

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

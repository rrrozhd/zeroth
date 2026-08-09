"""Concrete SQL persistence for threads.

A thread groups the runs of one ongoing conversation or task.
:class:`ThreadRepository` shares the low-level store with
:class:`~zeroth.integrations.persistence.runs.run_repository.RunRepository`
because thread and run writes are interleaved: registering a run creates its
thread, and writing a checkpoint updates the thread record.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from zeroth.integrations.persistence.runs.run_repository import (
    _merge,
    _merge_models,
    _RunThreadStore,
)
from zeroth.integrations.persistence.runs.serialization import row_to_thread
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase
from zeroth.runtime.runs import Thread, ThreadMemoryBinding, ThreadStatus

__all__ = ["ThreadRepository"]

_UNSCOPED_WORKSPACE = object()


class ThreadRepository:
    """High-level async interface for saving and loading threads.

    Provides methods for creating, updating, and querying threads,
    as well as attaching runs and managing the active run.
    """

    def __init__(self, database: AsyncDatabase):
        self._store = _RunThreadStore(database)

    async def create(self, thread: Thread) -> Thread:
        """Save a new thread and return the persisted version."""
        await self._store.create_thread(thread)
        return await self.get(thread.thread_id)

    async def get(
        self,
        thread_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> Thread | None:
        """Load a thread, optionally hiding scopes other than the caller's."""
        return await self._store.get_thread(
            thread_id,
            tenant_id=tenant_id,
            workspace_id=None if workspace_id is _UNSCOPED_WORKSPACE else workspace_id,
            workspace_scoped=workspace_id is not _UNSCOPED_WORKSPACE,
        )

    async def list(
        self,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> list[Thread]:
        """Return ordered threads, optionally constrained at query time."""
        sql = "SELECT * FROM threads"
        predicates: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            predicates.append("tenant_id = ?")
            params.append(tenant_id)
        if workspace_id is not _UNSCOPED_WORKSPACE:
            if workspace_id is None:
                predicates.append("workspace_id IS NULL")
            else:
                predicates.append("workspace_id = ?")
                params.append(workspace_id)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += " ORDER BY created_at, thread_id"
        async with self._store.database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
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

    async def attach_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> Thread:
        """Add a run to a thread and make it the active run."""
        thread = await self.get(
            thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if thread is None:
            raise KeyError(thread_id)
        if run_id not in thread.run_ids:
            thread.run_ids.append(run_id)
        thread.active_run_id = run_id
        thread.last_run_id = run_id
        thread.updated_at = utc_now()
        await self._store.save_thread(thread)
        return await self.get(
            thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    async def get_active_run_id(
        self,
        thread_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> str | None:
        """Return the currently active run ID for a thread."""
        thread = await self.get(thread_id, tenant_id=tenant_id, workspace_id=workspace_id)
        return None if thread is None else thread.active_run_id

    async def get_latest_run_id(
        self,
        thread_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> str | None:
        """Return the most recently added run ID for a thread."""
        thread = await self.get(thread_id, tenant_id=tenant_id, workspace_id=workspace_id)
        return None if thread is None else thread.last_run_id

    async def list_run_ids(
        self,
        thread_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED_WORKSPACE,
    ) -> list[str]:
        """Return all run IDs belonging to a thread."""
        thread = await self.get(thread_id, tenant_id=tenant_id, workspace_id=workspace_id)
        return [] if thread is None else list(thread.run_ids)

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run as the active run for its thread."""
        await self._store.set_active_run_id(thread_id, run_id)

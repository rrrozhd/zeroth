"""Database-backed thread resolution and state storage.

This module provides thread management backed by a real database (SQLite).
Use these classes when you need thread state to survive process restarts,
unlike the in-memory store which loses data when the process stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from zeroth.runs.models import Run, Thread, ThreadMemoryBinding, ThreadStatus
from zeroth.runs.repository import RunRepository, ThreadRepository
from zeroth.storage import SQLiteDatabase
from zeroth.storage.json import to_json_value

THREAD_STATE_CHECKPOINT_KIND = "thread_state"
THREAD_STATE_METADATA_KEY = "thread_state"
THREAD_STATE_KIND_KEY = "checkpoint_kind"


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _new_checkpoint_id() -> str:
    """Generate a unique ID for a new checkpoint."""
    return uuid4().hex


class ThreadResolution(BaseModel):
    """The result of looking up or creating a thread.

    Tells you which thread was found (or created), whether it was
    newly created, and what state was restored from a previous run.
    """

    model_config = ConfigDict(extra="forbid")

    thread: Thread
    created: bool = False
    restored_state: dict[str, Any] | None = None
    checkpoint_id: str | None = None


@dataclass(slots=True)
class RepositoryThreadResolver:
    """Finds an existing thread or creates a new one in the database.

    Use this when you need to look up a thread by ID, creating it
    automatically if it does not exist yet.
    """

    thread_repository: ThreadRepository

    def resolve(
        self,
        thread_id: str | None,
        *,
        graph_version_ref: str,
        deployment_ref: str,
        participating_agent_refs: list[str] | None = None,
        state_snapshot_refs: list[str] | None = None,
        checkpoint_refs: list[str] | None = None,
        memory_bindings: list[ThreadMemoryBinding] | None = None,
        run_id: str | None = None,
        status: ThreadStatus | None = None,
    ) -> ThreadResolution:
        """Look up a thread by ID, or create one if it does not exist yet."""
        created = thread_id is None or self.thread_repository.get(thread_id) is None
        thread = self.thread_repository.resolve(
            thread_id,
            graph_version_ref=graph_version_ref,
            deployment_ref=deployment_ref,
            participating_agent_refs=participating_agent_refs,
            state_snapshot_refs=state_snapshot_refs,
            checkpoint_refs=checkpoint_refs,
            memory_bindings=memory_bindings,
            run_id=run_id,
            status=status,
        )
        return ThreadResolution(thread=thread, created=created)

    def resolve_optional(self, thread_id: str | None, **kwargs: Any) -> ThreadResolution | None:
        """Like resolve, but returns None if no thread ID is provided."""
        if thread_id is None:
            return None
        return self.resolve(thread_id, **kwargs)


class RepositoryThreadStateStore:
    """Saves and loads thread state using the database.

    Each state save creates a checkpoint record in the database, so you
    can trace the full history of a thread's state over time. This is
    the production-grade alternative to InMemoryThreadStateStore.
    """

    def __init__(
        self,
        database: SQLiteDatabase | None = None,
        *,
        run_repository: RunRepository | None = None,
        thread_repository: ThreadRepository | None = None,
    ) -> None:
        if run_repository is None or thread_repository is None:
            if database is None:
                raise ValueError("database or repository instances are required")
            run_repository = run_repository or RunRepository(database)
            thread_repository = thread_repository or ThreadRepository(database)
        if database is None:
            database = self._database_from_repositories(run_repository, thread_repository)
        self._run_repository = run_repository
        self._thread_repository = thread_repository
        self._database = database

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """Load the most recent state snapshot for a thread from the database."""
        thread = self._thread_repository.get(thread_id)
        if thread is None:
            return None
        for checkpoint_id in reversed(thread.state_snapshot_refs or thread.checkpoint_refs):
            checkpoint = self._run_repository.get_checkpoint(checkpoint_id)
            if checkpoint is None:
                continue
            if not self._is_thread_state_checkpoint(checkpoint):
                continue
            return self._extract_state(checkpoint)
        return None

    async def load_optional(self, thread_id: str | None) -> dict[str, Any] | None:
        """Like load, but returns None if no thread ID is provided."""
        if thread_id is None:
            return None
        return await self.load(thread_id)

    async def checkpoint(self, thread_id: str, state: dict[str, Any]) -> str:
        """Save a state snapshot for the thread and return the checkpoint ID."""
        thread = self._thread_repository.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)

        checkpoint_id = _new_checkpoint_id()
        checkpoint = self._build_checkpoint(thread, state, checkpoint_id=checkpoint_id)
        self._write_checkpoint(checkpoint, checkpoint_id=checkpoint_id)
        self._thread_repository.resolve(
            thread_id,
            graph_version_ref=thread.graph_version_ref,
            deployment_ref=thread.deployment_ref,
            state_snapshot_refs=[checkpoint_id],
            checkpoint_refs=[checkpoint_id],
            status=thread.status,
        )
        return checkpoint_id

    async def checkpoint_optional(self, thread_id: str | None, state: dict[str, Any]) -> str | None:
        """Like checkpoint, but does nothing and returns None if no thread ID is provided."""
        if thread_id is None:
            return None
        return await self.checkpoint(thread_id, state)

    def _build_checkpoint(
        self,
        thread: Thread,
        state: dict[str, Any],
        *,
        checkpoint_id: str,
    ) -> Run:
        """Create a Run object that represents a thread state checkpoint."""
        run = Run(
            run_id=f"thread-state:{thread.thread_id}:{checkpoint_id}",
            workflow_name=f"thread-state:{thread.thread_id}",
            graph_version_ref=thread.graph_version_ref,
            deployment_ref=thread.deployment_ref,
            metadata={
                THREAD_STATE_KIND_KEY: THREAD_STATE_CHECKPOINT_KIND,
                THREAD_STATE_METADATA_KEY: dict(state),
                "thread_id": thread.thread_id,
                "created_at": _utc_now().isoformat(),
            },
        )
        run.checkpoint_id = checkpoint_id
        run.touch()
        return run

    def _write_checkpoint(self, run: Run, *, checkpoint_id: str) -> None:
        """Write the checkpoint record to the database."""
        snapshot = run.model_dump(mode="json")
        checkpoint_order = self._checkpoint_order(run.thread_id)
        if self._database is None:
            raise ValueError("database is required to persist checkpoints")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                    checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    thread_id = excluded.thread_id,
                    checkpoint_order = excluded.checkpoint_order,
                    state_json = excluded.state_json,
                    created_at = excluded.created_at
                """,
                (
                    checkpoint_id,
                    run.run_id,
                    run.thread_id,
                    checkpoint_order,
                    to_json_value(snapshot),
                    run.updated_at.isoformat(),
                ),
            )

    def _checkpoint_order(self, thread_id: str) -> int:
        """Return the next sequential order number for checkpoints on this thread."""
        thread = self._thread_repository.get(thread_id)
        if thread is None:
            return 0
        return len(thread.checkpoint_refs)

    def _database_from_repositories(
        self,
        run_repository: RunRepository,
        thread_repository: ThreadRepository,
    ) -> SQLiteDatabase | None:
        """Try to find the database instance from the given repositories."""
        for repository in (run_repository, thread_repository):
            store = getattr(repository, "_store", None)
            database = getattr(store, "database", None)
            if database is not None:
                return database
        return None

    def _is_thread_state_checkpoint(self, checkpoint: Run) -> bool:
        """Check whether a checkpoint record is a thread state checkpoint."""
        return checkpoint.metadata.get(THREAD_STATE_KIND_KEY) == THREAD_STATE_CHECKPOINT_KIND

    def _extract_state(self, checkpoint: Run) -> dict[str, Any] | None:
        """Pull the thread state data out of a checkpoint record."""
        state = checkpoint.metadata.get(THREAD_STATE_METADATA_KEY)
        if isinstance(state, dict):
            return dict(state)
        return None

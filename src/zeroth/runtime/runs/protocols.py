"""Repository protocols the runtime depends on to execute a graph.

Each protocol is deliberately narrower than the shipped repository classes: it
declares only the operations runtime code actually performs. Orchestration
persists a run (:class:`RunWriter`), reloads one to resume (:class:`RunReader`),
snapshots progress (:class:`CheckpointStore`), and upserts the owning thread
(:class:`ThreadStore`). Status transitions, history recording, listing, and
retention queries stay on the concrete repositories because the runtime never
calls them — it mutates the run in memory and persists the whole object.

Keeping these contracts small is what lets persistence be replaced without
reimplementing the full repository API.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from zeroth.runtime.runs.models import Run, RunStatus, Thread, ThreadMemoryBinding, ThreadStatus

__all__ = ["CheckpointStore", "RunReader", "RunWriter", "ThreadStore"]


class RunReader(Protocol):
    """Load persisted runs by identity."""

    async def get(self, run_id: str) -> Run | None:
        """Load a run by its ID, or return None if not found."""
        ...


class RunWriter(Protocol):
    """Persist runs as execution progresses."""

    async def create(self, run: Run) -> Run:
        """Save a new run and return the persisted version."""
        ...

    async def put(self, run: Run) -> Run:
        """Save (insert or update) a run and return the persisted version."""
        ...

    async def put_if_status(self, run: Run, expected_status: RunStatus) -> Run:
        """Save a run only while its persisted status matches the snapshot."""
        ...


class CheckpointStore(Protocol):
    """Snapshot run state so an interrupted execution can resume."""

    async def write_checkpoint(self, run: Run) -> str:
        """Save a snapshot of the run and return the checkpoint ID."""
        ...

    async def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        """Load a checkpoint by its ID."""
        ...


class ThreadStore(Protocol):
    """Resolve and update the thread that groups a sequence of runs."""

    async def get(self, thread_id: str) -> Thread | None:
        """Load a thread by its ID, or return None if not found."""
        ...

    async def resolve(
        self,
        thread_id: str | None,
        *,
        graph_version_ref: str,
        deployment_ref: str,
        participating_agent_refs: Sequence[str] | None = None,
        state_snapshot_refs: Sequence[str] | None = None,
        checkpoint_refs: Sequence[str] | None = None,
        memory_bindings: Sequence[ThreadMemoryBinding] | None = None,
        run_id: str | None = None,
        status: ThreadStatus | None = None,
    ) -> Thread:
        """Find or create a thread, merging in any new data."""
        ...

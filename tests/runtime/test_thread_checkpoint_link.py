"""ZER-49 A06-12: a thread checkpoint that is written but never linked must be loud.

``RepositoryThreadStateStore.checkpoint`` writes the checkpoint row and then
links it onto the thread in a second, independent statement. Nothing makes the
pair atomic (and nothing in this codebase can: an outer ``database.transaction``
opens a *second* connection, so it is two transactions). The failure that must
never be silent is the one where the write lands and the link does not — the
state is then unreachable from ``load`` while the caller was told the
checkpoint id was saved. ``ThreadRepository.resolve`` is itself a
read-modify-write of the whole thread row, so a concurrent resolve dropping our
ref is exactly that case.
"""

from __future__ import annotations

import logging

import pytest

from zeroth.runtime.agents.thread_store import (
    RepositoryThreadStateStore,
    ThreadCheckpointLinkError,
)
from zeroth.runtime.runs import Run, Thread


class _CollectingRunRepository:
    def __init__(self) -> None:
        self.checkpoints: list[Run] = []

    async def write_checkpoint(self, run: Run) -> str:
        self.checkpoints.append(run)
        return run.checkpoint_id or ""


class _MergingThreadRepository:
    """Healthy behaviour: resolve merges the incoming refs onto the thread."""

    def __init__(self, thread: Thread) -> None:
        self.thread = thread

    async def get(self, thread_id: str) -> Thread | None:
        return self.thread if thread_id == self.thread.thread_id else None

    async def resolve(self, thread_id: str, **kwargs) -> Thread:
        for ref in kwargs.get("state_snapshot_refs") or []:
            if ref not in self.thread.state_snapshot_refs:
                self.thread.state_snapshot_refs = [*self.thread.state_snapshot_refs, ref]
        for ref in kwargs.get("checkpoint_refs") or []:
            if ref not in self.thread.checkpoint_refs:
                self.thread.checkpoint_refs = [*self.thread.checkpoint_refs, ref]
        return self.thread


class _LosingThreadRepository(_MergingThreadRepository):
    """A concurrent writer's read-modify-write clobbered our ref."""

    async def resolve(self, thread_id: str, **kwargs) -> Thread:
        return self.thread


class _RaisingThreadRepository(_MergingThreadRepository):
    async def resolve(self, thread_id: str, **kwargs) -> Thread:
        raise KeyError("thread could not be resolved")


def _thread() -> Thread:
    return Thread(thread_id="t-1", graph_version_ref="g:v1", deployment_ref="d")


def _store(thread_repository) -> RepositoryThreadStateStore:
    return RepositoryThreadStateStore(
        None,
        tenant_id="default",
        workspace_id=None,
        run_repository=_CollectingRunRepository(),
        thread_repository=thread_repository,
    )


async def test_a_checkpoint_the_thread_never_referenced_is_an_error() -> None:
    thread_repository = _LosingThreadRepository(_thread())
    store = _store(thread_repository)

    with pytest.raises(ThreadCheckpointLinkError) as caught:
        await store.checkpoint("t-1", {"turn": 1})

    assert "t-1" in str(caught.value)
    assert thread_repository.thread.state_snapshot_refs == []


async def test_a_linked_checkpoint_returns_its_id() -> None:
    thread_repository = _MergingThreadRepository(_thread())
    store = _store(thread_repository)

    checkpoint_id = await store.checkpoint("t-1", {"turn": 1})

    assert thread_repository.thread.state_snapshot_refs == [checkpoint_id]
    assert thread_repository.thread.checkpoint_refs == [checkpoint_id]


async def test_a_failed_link_keeps_its_exception_and_names_the_orphan(caplog) -> None:
    """The caller's exception contract is unchanged; the orphan is now traceable."""
    store = _store(_RaisingThreadRepository(_thread()))

    with caplog.at_level(logging.WARNING, logger="zeroth.runtime.agents.thread_store"):
        with pytest.raises(KeyError):
            await store.checkpoint("t-1", {"turn": 1})

    assert any("t-1" in record.getMessage() for record in caplog.records)

"""Characterization tests for the canonical thread persistence adapter.

Thread resolution is the merge-heavy half of run persistence: ``resolve``
either creates a thread or folds new references into an existing one, and it
refuses to do so when the caller's identity fields disagree with what was
stored. These pin that behaviour across the move.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.runtime.runs import Thread, ThreadMemoryBinding, ThreadStatus


def test_thread_repository_imports_in_a_cold_interpreter() -> None:
    """The thread adapter must import without ``zeroth.core`` warmed first."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from zeroth.integrations.persistence.runs import ThreadRepository",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"ThreadRepository is not cold-importable:\n{result.stderr}"


def _make_thread(thread_id: str = "thread-1", *, tenant_id: str = "tenant-1") -> Thread:
    return Thread(
        thread_id=thread_id,
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        status=ThreadStatus.ACTIVE,
    )


async def test_create_persists_a_thread_and_reads_it_back(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A created thread round-trips through the database."""
    repository = ThreadRepository(sqlite_db)

    created = await repository.create(_make_thread())

    assert created is not None
    assert created.thread_id == "thread-1"
    assert (await repository.get("thread-1")) is not None


async def test_get_returns_none_for_an_unknown_thread(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Missing threads are absent, not an error."""
    repository = ThreadRepository(sqlite_db)

    assert await repository.get("missing") is None


async def test_list_returns_threads_in_creation_order(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Listing is ordered so paging over threads is stable."""
    repository = ThreadRepository(sqlite_db)
    await repository.create(_make_thread("thread-a"))
    await repository.create(_make_thread("thread-b"))

    listed = await repository.list()

    assert [thread.thread_id for thread in listed] == ["thread-a", "thread-b"]


async def test_update_persists_changes_and_advances_the_timestamp(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Updating a thread stamps it so staleness checks stay meaningful."""
    repository = ThreadRepository(sqlite_db)
    created = await repository.create(_make_thread())
    assert created is not None
    original_updated_at = created.updated_at

    created.status = ThreadStatus.COMPLETED
    updated = await repository.update(created)

    assert updated is not None
    assert updated.status is ThreadStatus.COMPLETED
    assert updated.updated_at >= original_updated_at


async def test_resolve_creates_a_thread_that_does_not_exist_yet(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Resolution is get-or-create, so first use of a thread id succeeds."""
    repository = ThreadRepository(sqlite_db)

    resolved = await repository.resolve(
        "thread-new",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )

    assert resolved.thread_id == "thread-new"
    assert resolved.run_ids == ["run-1"]
    assert resolved.active_run_id == "run-1"


async def test_resolve_without_a_thread_id_falls_back_to_the_run_id(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A run with no thread gets a thread named after itself, not a random one."""
    repository = ThreadRepository(sqlite_db)

    resolved = await repository.resolve(
        None,
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        run_id="run-7",
    )

    assert resolved.thread_id == "run-7"


async def test_resolve_merges_references_without_duplicating_them(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Repeated resolution accumulates references instead of overwriting them."""
    repository = ThreadRepository(sqlite_db)
    binding = ThreadMemoryBinding(connector_id="conn-1", instance_id="memory-1")
    await repository.resolve(
        "thread-1",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        participating_agent_refs=["agent-1"],
        checkpoint_refs=["checkpoint-1"],
        memory_bindings=[binding],
        run_id="run-1",
    )

    resolved = await repository.resolve(
        "thread-1",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        participating_agent_refs=["agent-1", "agent-2"],
        checkpoint_refs=["checkpoint-1", "checkpoint-2"],
        memory_bindings=[binding],
        run_id="run-2",
    )

    assert resolved.participating_agent_refs == ["agent-1", "agent-2"]
    assert resolved.checkpoint_refs == ["checkpoint-1", "checkpoint-2"]
    assert resolved.memory_bindings == [binding]
    assert resolved.run_ids == ["run-1", "run-2"]
    assert resolved.active_run_id == "run-2"


async def test_resolve_rejects_a_thread_identity_mismatch(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A thread may not be re-pointed at another tenant or deployment."""
    repository = ThreadRepository(sqlite_db)
    await repository.resolve(
        "thread-1",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
    )

    with pytest.raises(ValueError, match="thread identity mismatch"):
        await repository.resolve(
            "thread-1",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-2",
        )


async def test_attach_run_makes_it_the_active_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Attaching a run records it and marks it current."""
    repository = ThreadRepository(sqlite_db)
    await repository.create(_make_thread())

    attached = await repository.attach_run("thread-1", "run-1")

    assert attached is not None
    assert attached.run_ids == ["run-1"]
    assert attached.active_run_id == "run-1"
    assert await repository.get_active_run_id("thread-1") == "run-1"
    assert await repository.get_latest_run_id("thread-1") == "run-1"
    assert await repository.list_run_ids("thread-1") == ["run-1"]


async def test_attach_run_raises_for_an_unknown_thread(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Attaching to a thread that does not exist is a KeyError, not a silent create."""
    repository = ThreadRepository(sqlite_db)

    with pytest.raises(KeyError):
        await repository.attach_run("missing", "run-1")

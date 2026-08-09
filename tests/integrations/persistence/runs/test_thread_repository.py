"""Characterization tests for the canonical thread persistence adapter.

Thread resolution is the merge-heavy half of run persistence: ``resolve``
either creates a thread or folds new references into an existing one, and it
refuses to do so when the caller's identity fields disagree with what was
stored. These pin that behaviour across the move.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.runtime.runs import Run, Thread, ThreadMemoryBinding, ThreadStatus


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


def _make_thread(
    thread_id: str = "thread-1",
    *,
    tenant_id: str = "tenant-1",
    workspace_id: str | None = None,
) -> Thread:
    return Thread(
        thread_id=thread_id,
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
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


async def test_resolve_same_logical_id_creates_an_independent_tenant_thread(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """A logical external ID is independently addressable in each tenant."""
    repository = ThreadRepository(sqlite_db)
    await repository.resolve(
        "thread-1",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-1",
    )

    other = await repository.resolve(
        "thread-1",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-2",
    )
    assert other.tenant_id == "tenant-2"


async def test_attach_run_makes_it_the_active_run(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    """Attaching a run records it and marks it current."""
    repository = ThreadRepository(sqlite_db)
    await repository.create(_make_thread())
    await RunRepository(sqlite_db).create(
        Run(
            run_id="run-1",
            thread_id="run-1-origin",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-1",
        )
    )

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


async def test_scoped_get_hides_foreign_thread_like_unknown(sqlite_db: AsyncSQLiteDatabase) -> None:
    repository = ThreadRepository(sqlite_db)
    await repository.create(
        _make_thread("owned-thread", tenant_id="tenant-a", workspace_id="workspace-a")
    )

    foreign = await repository.get("owned-thread", tenant_id="tenant-b", workspace_id="workspace-a")
    unknown = await repository.get(
        "unknown-thread", tenant_id="tenant-b", workspace_id="workspace-a"
    )

    assert foreign is unknown is None


async def test_create_allows_scoped_same_thread_id_without_overwriting_owner(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = ThreadRepository(sqlite_db)
    await repository.create(
        _make_thread("shared-id", tenant_id="tenant-a", workspace_id="workspace-a")
    )

    created = await repository.create(
        _make_thread("shared-id", tenant_id="tenant-b", workspace_id="workspace-b")
    )
    assert created.tenant_id == "tenant-b"

    owner = await repository.get("shared-id", tenant_id="tenant-a", workspace_id="workspace-a")
    assert owner is not None
    assert owner.tenant_id == "tenant-a"
    assert owner.workspace_id == "workspace-a"


async def test_scoped_list_excludes_other_tenant_and_workspace(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = ThreadRepository(sqlite_db)
    await repository.create(_make_thread("a-1", tenant_id="tenant-a", workspace_id="workspace-a"))
    await repository.create(_make_thread("a-2", tenant_id="tenant-a", workspace_id="workspace-b"))
    await repository.create(_make_thread("b-1", tenant_id="tenant-b", workspace_id="workspace-a"))

    listed = await repository.list(tenant_id="tenant-a", workspace_id="workspace-a")
    foreign = await repository.list(tenant_id="tenant-b", workspace_id="workspace-b")
    unknown = await repository.list(tenant_id="tenant-unknown", workspace_id="workspace-unknown")

    assert [thread.thread_id for thread in listed] == ["a-1"]
    assert foreign == unknown == []


async def test_scoped_attach_hides_foreign_thread_like_unknown(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = ThreadRepository(sqlite_db)
    runs = RunRepository(sqlite_db)
    await repository.create(
        _make_thread("owned-thread", tenant_id="tenant-a", workspace_id="workspace-a")
    )

    for thread_id in ("owned-thread", "unknown-thread"):
        with pytest.raises(KeyError) as raised:
            await repository.attach_run(
                thread_id,
                "run-b",
                tenant_id="tenant-b",
                workspace_id="workspace-a",
            )
        assert raised.value.args == (thread_id,)

    await runs.create(
        Run(
            run_id="run-a",
            thread_id="owned-thread",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    owner = await repository.attach_run(
        "owned-thread",
        "run-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    assert owner.run_ids == ["run-a"]


async def test_scoped_attach_rejects_foreign_and_unknown_runs_identically(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    threads = ThreadRepository(sqlite_db)
    runs = RunRepository(sqlite_db)
    await threads.create(
        _make_thread("tenant-a-thread", tenant_id="tenant-a", workspace_id="workspace-a")
    )
    await runs.create(
        Run(
            run_id="tenant-b-run",
            thread_id="tenant-b-thread",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-b",
            workspace_id="workspace-b",
        )
    )

    for run_id in ("tenant-b-run", "unknown-run"):
        with pytest.raises(KeyError) as raised:
            await threads.attach_run(
                "tenant-a-thread",
                run_id,
                tenant_id="tenant-a",
                workspace_id="workspace-a",
            )
        assert raised.value.args == (run_id,)


async def test_scoped_resolve_foreign_id_matches_unknown_create_semantics(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = ThreadRepository(sqlite_db)
    await repository.create(
        _make_thread("external-id", tenant_id="tenant-a", workspace_id="workspace-a")
    )

    foreign_collision = await repository.resolve(
        "external-id",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-b",
        workspace_id="workspace-b",
        participating_agent_refs=["tenant-b-agent"],
    )
    unknown = await repository.resolve(
        "unknown-external-id",
        graph_version_ref="graph-1",
        deployment_ref="deployment-1",
        tenant_id="tenant-b",
        workspace_id="workspace-b",
        participating_agent_refs=["tenant-b-agent"],
    )

    for thread in (foreign_collision, unknown):
        assert thread.tenant_id == "tenant-b"
        assert thread.workspace_id == "workspace-b"
        assert thread.graph_version_ref == "graph-1"
        assert thread.deployment_ref == "deployment-1"
        assert thread.participating_agent_refs == ["tenant-b-agent"]

    owner = await repository.get("external-id", tenant_id="tenant-a", workspace_id="workspace-a")
    assert owner is not None
    assert owner.participating_agent_refs == []


async def test_same_external_thread_id_race_creates_one_thread_per_tenant(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "thread-scope-race.db"
    run_migrations(f"sqlite:///{database_path}")
    database = AsyncSQLiteDatabase(str(database_path))
    first = ThreadRepository(database)
    second = ThreadRepository(database)

    results = await asyncio.gather(
        first.resolve(
            "raced-external-id",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        ),
        second.resolve(
            "raced-external-id",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-b",
            workspace_id="workspace-b",
        ),
    )

    assert {thread.tenant_id for thread in results} == {"tenant-a", "tenant-b"}
    await database.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        repository = ThreadRepository(restarted)
        assert (
            await repository.get(
                "raced-external-id", tenant_id="tenant-a", workspace_id="workspace-a"
            )
            is not None
        )
        assert (
            await repository.get(
                "raced-external-id", tenant_id="tenant-b", workspace_id="workspace-b"
            )
            is not None
        )
    finally:
        await restarted.close()


async def test_scoped_active_run_helpers_hide_foreign_thread(
    sqlite_db: AsyncSQLiteDatabase,
) -> None:
    repository = ThreadRepository(sqlite_db)
    await repository.create(
        _make_thread("owned-thread", tenant_id="tenant-a", workspace_id="workspace-a")
    )
    await RunRepository(sqlite_db).create(
        Run(
            run_id="run-a",
            thread_id="run-a-origin",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    await repository.attach_run("owned-thread", "run-a")

    assert (
        await repository.get_active_run_id(
            "owned-thread", tenant_id="tenant-b", workspace_id="workspace-a"
        )
        is None
    )
    assert (
        await repository.get_latest_run_id(
            "owned-thread", tenant_id="tenant-b", workspace_id="workspace-a"
        )
        is None
    )
    assert (
        await repository.list_run_ids(
            "owned-thread", tenant_id="tenant-b", workspace_id="workspace-a"
        )
        == []
    )


@pytest.mark.parametrize("operation", ["attach", "set-active", "run-repository-set-active"])
async def test_unscoped_thread_run_link_rejects_foreign_run_without_mutation(
    sqlite_db: AsyncSQLiteDatabase, operation: str
) -> None:
    threads = ThreadRepository(sqlite_db)
    runs = RunRepository(sqlite_db)
    await threads.create(_make_thread("thread-a", tenant_id="tenant-a"))
    await runs.create(
        Run(
            run_id="run-b",
            thread_id="thread-b",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-b",
        )
    )

    with pytest.raises(KeyError):
        if operation == "attach":
            await threads.attach_run("thread-a", "run-b")
        elif operation == "set-active":
            await threads.set_active_run_id("thread-a", "run-b")
        else:
            await runs.set_active_run_id("thread-a", "run-b")

    owner = await threads.get("thread-a", tenant_id="tenant-a", workspace_id=None)
    assert owner is not None
    assert owner.run_ids == []
    assert owner.active_run_id is None


async def test_valid_thread_run_link_survives_repository_restart(sqlite_db) -> None:
    threads = ThreadRepository(sqlite_db)
    runs = RunRepository(sqlite_db)
    await threads.create(_make_thread("thread-a", tenant_id="tenant-a"))
    await runs.create(
        Run(
            run_id="run-a",
            thread_id="run-origin",
            graph_version_ref="graph-1",
            deployment_ref="deployment-1",
            tenant_id="tenant-a",
        )
    )

    await threads.set_active_run_id("thread-a", "run-a")
    reopened = ThreadRepository(sqlite_db)
    persisted = await reopened.get("thread-a", tenant_id="tenant-a", workspace_id=None)
    assert persisted is not None
    assert persisted.run_ids == ["run-a"]
    assert persisted.active_run_id == "run-a"


async def test_scoped_set_active_addresses_duplicate_logical_thread_ids(sqlite_db) -> None:
    threads = ThreadRepository(sqlite_db)
    runs = RunRepository(sqlite_db)
    for tenant in ("tenant-a", "tenant-b"):
        await threads.create(_make_thread("shared-thread", tenant_id=tenant))
        await runs.create(
            Run(
                run_id=f"run-{tenant}",
                thread_id=f"origin-{tenant}",
                graph_version_ref="graph-1",
                deployment_ref="deployment-1",
                tenant_id=tenant,
            )
        )

    await threads.set_active_run_id(
        "shared-thread", "run-tenant-a", tenant_id="tenant-a", workspace_id=None
    )
    await runs.set_active_run_id(
        "shared-thread", "run-tenant-b", tenant_id="tenant-b", workspace_id=None
    )

    owner_a = await threads.get("shared-thread", tenant_id="tenant-a", workspace_id=None)
    owner_b = await threads.get("shared-thread", tenant_id="tenant-b", workspace_id=None)
    assert owner_a is not None and owner_a.active_run_id == "run-tenant-a"
    assert owner_b is not None and owner_b.active_run_id == "run-tenant-b"

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import zeroth.runtime.agents.thread_store as thread_store
from zeroth.runtime.agents.thread_store import (
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
)
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.service.bootstrap.migrations import run_migrations
import pytest


def test_thread_resolver_scope_is_constructor_bound() -> None:
    parameters = inspect.signature(RepositoryThreadResolver.resolve).parameters

    assert "tenant_id" not in parameters
    assert "workspace_id" not in parameters


async def test_thread_resolver_creates_and_continues_thread(sqlite_db) -> None:
    resolver = RepositoryThreadResolver(ThreadRepository.for_default_compatibility(sqlite_db))

    created = await resolver.resolve(
        None,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        participating_agent_refs=["agent-a"],
        state_snapshot_refs=["snapshot-a"],
        checkpoint_refs=["checkpoint-a"],
        run_id="run-a",
    )
    continued = await resolver.resolve(
        created.thread.thread_id,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        participating_agent_refs=["agent-b"],
        state_snapshot_refs=["snapshot-b"],
        checkpoint_refs=["checkpoint-b"],
        run_id="run-b",
    )

    assert created.created is True
    assert continued.created is False
    assert created.thread.thread_id == continued.thread.thread_id
    assert continued.thread.run_ids == ["run-a", "run-b"]
    assert continued.thread.participating_agent_refs == ["agent-a", "agent-b"]
    assert continued.thread.state_snapshot_refs == ["snapshot-a", "snapshot-b"]
    assert continued.thread.checkpoint_refs == ["checkpoint-a", "checkpoint-b"]
    assert continued.thread.last_run_id == "run-b"
    assert continued.thread.active_run_id == "run-b"


async def test_thread_resolver_prelookup_uses_requested_scope(sqlite_db, monkeypatch) -> None:
    repository = ThreadRepository(
        sqlite_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    await repository.resolve(
        "foreign-or-unknown",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
    )
    resolver = RepositoryThreadResolver(repository)
    calls: list[dict[str, object]] = []
    original_get = repository.get

    async def recording_get(thread_id: str, **scope):
        calls.append(scope)
        return await original_get(thread_id, **scope)

    monkeypatch.setattr(repository, "get", recording_get)
    resolved = await resolver.resolve(
        "foreign-or-unknown",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
    )

    assert calls[0] == {}
    assert resolved.thread.tenant_id == "tenant-a"
    assert resolved.thread.workspace_id == "workspace-a"


async def test_thread_state_store_checkpoints_and_loads_latest_state(
    sqlite_db,
    monkeypatch,
) -> None:
    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(thread_store, "utc_now", lambda: fixed)
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    thread_repository = ThreadRepository.for_default_compatibility(sqlite_db)
    resolver = RepositoryThreadResolver(thread_repository)
    created = await resolver.resolve(
        None,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        run_id="run-a",
    )
    store = RepositoryThreadStateStore(
        sqlite_db,
        tenant_id="default",
        workspace_id=None,
        run_repository=run_repository,
        thread_repository=thread_repository,
    )

    first_checkpoint = await store.checkpoint(
        created.thread.thread_id,
        {"step": 1, "secret": "top-secret"},
    )
    second_checkpoint = await store.checkpoint(
        created.thread.thread_id,
        {"step": 2, "nested": {"token": "abc"}},
    )

    loaded = await store.load(created.thread.thread_id)
    thread = await thread_repository.get(created.thread.thread_id)
    latest_checkpoint = await run_repository.get_checkpoint(second_checkpoint)

    assert first_checkpoint != second_checkpoint
    assert loaded == {"step": 2, "nested": {"token": "abc"}}
    assert thread is not None
    assert thread.run_ids == ["run-a"]
    assert thread.state_snapshot_refs == [first_checkpoint, second_checkpoint]
    assert thread.checkpoint_refs == [first_checkpoint, second_checkpoint]
    assert latest_checkpoint is not None
    assert latest_checkpoint.metadata["checkpoint_kind"] == "thread_state"
    assert latest_checkpoint.metadata["thread_state"] == {"step": 2, "nested": {"token": "abc"}}
    assert latest_checkpoint.metadata["created_at"] == fixed.isoformat()
    assert latest_checkpoint.audit_refs == []
    assert latest_checkpoint.final_output is None


async def test_thread_store_noop_helpers_without_thread_id(sqlite_db) -> None:
    resolver = RepositoryThreadResolver(ThreadRepository.for_default_compatibility(sqlite_db))
    store = RepositoryThreadStateStore(sqlite_db, tenant_id="default", workspace_id=None)

    assert (
        await resolver.resolve_optional(
            None,
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
        is None
    )
    assert await store.load_optional(None) is None
    assert await store.checkpoint_optional(None, {"step": 1}) is None


async def test_thread_state_store_same_id_isolated_by_tenant_checkpoint_scope(sqlite_db) -> None:
    for tenant in ("tenant-a", "tenant-b"):
        await ThreadRepository(sqlite_db, NullWorkspaceScopeContext(tenant_id=tenant)).resolve(
            "shared-state-id",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    store_a = RepositoryThreadStateStore(sqlite_db, tenant_id="tenant-a", workspace_id=None)
    store_b = RepositoryThreadStateStore(sqlite_db, tenant_id="tenant-b", workspace_id=None)

    await store_a.checkpoint("shared-state-id", {"owner": "a"})
    assert await store_a.load("shared-state-id") == {"owner": "a"}
    assert await store_b.load("shared-state-id") is None
    await store_b.checkpoint("shared-state-id", {"owner": "b"})
    assert await store_a.load("shared-state-id") == {"owner": "a"}
    assert await store_b.load("shared-state-id") == {"owner": "b"}


async def test_thread_state_checkpoint_owner_survives_shadow_id_and_restart(tmp_path) -> None:
    database_path = tmp_path / "checkpoint-owner.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    for tenant in ("tenant-a", "tenant-b"):
        await ThreadRepository(first, NullWorkspaceScopeContext(tenant_id=tenant)).resolve(
            "shadow-id",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    owner = RepositoryThreadStateStore(first, tenant_id="tenant-a", workspace_id=None)
    checkpoint_id = await owner.checkpoint("shadow-id", {"secret": "tenant-a-only"})
    tenant_b_threads = ThreadRepository(first, NullWorkspaceScopeContext(tenant_id="tenant-b"))
    await tenant_b_threads.resolve(
        f"thread-state:shadow-id:{checkpoint_id}",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
    )
    await tenant_b_threads.resolve(
        "shadow-id",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        state_snapshot_refs=[checkpoint_id, "unknown-checkpoint"],
        checkpoint_refs=[checkpoint_id, "unknown-checkpoint"],
    )
    await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        owner_after_restart = RepositoryThreadStateStore(
            restarted, tenant_id="tenant-a", workspace_id=None
        )
        foreign = RepositoryThreadStateStore(restarted, tenant_id="tenant-b", workspace_id=None)

        assert await owner_after_restart.load("shadow-id") == {"secret": "tenant-a-only"}
        assert await foreign.load("shadow-id") is None
    finally:
        await restarted.close()

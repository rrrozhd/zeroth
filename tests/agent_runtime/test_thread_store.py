from __future__ import annotations

from datetime import UTC, datetime

import zeroth.runtime.agents.thread_store as thread_store
from zeroth.runtime.agents.thread_store import (
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
)
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
import pytest


async def test_thread_resolver_creates_and_continues_thread(sqlite_db) -> None:
    resolver = RepositoryThreadResolver(ThreadRepository(sqlite_db))

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
    repository = ThreadRepository(sqlite_db)
    await repository.resolve(
        "foreign-or-unknown",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
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
        tenant_id="tenant-b",
        workspace_id="workspace-b",
    )

    assert calls[0] == {"tenant_id": "tenant-b", "workspace_id": "workspace-b"}
    assert resolved.thread.tenant_id == "tenant-b"
    assert resolved.thread.workspace_id == "workspace-b"


async def test_thread_state_store_checkpoints_and_loads_latest_state(
    sqlite_db,
    monkeypatch,
) -> None:
    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(thread_store, "utc_now", lambda: fixed)
    run_repository = RunRepository(sqlite_db)
    thread_repository = ThreadRepository(sqlite_db)
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
    resolver = RepositoryThreadResolver(ThreadRepository(sqlite_db))
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
    threads = ThreadRepository(sqlite_db)
    for tenant in ("tenant-a", "tenant-b"):
        await threads.resolve(
            "shared-state-id",
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            tenant_id=tenant,
        )
    store_a = RepositoryThreadStateStore(sqlite_db, tenant_id="tenant-a", workspace_id=None)
    store_b = RepositoryThreadStateStore(sqlite_db, tenant_id="tenant-b", workspace_id=None)

    await store_a.checkpoint("shared-state-id", {"owner": "a"})
    assert await store_a.load("shared-state-id") == {"owner": "a"}
    assert await store_b.load("shared-state-id") is None
    await store_b.checkpoint("shared-state-id", {"owner": "b"})
    assert await store_a.load("shared-state-id") == {"owner": "a"}
    assert await store_b.load("shared-state-id") == {"owner": "b"}

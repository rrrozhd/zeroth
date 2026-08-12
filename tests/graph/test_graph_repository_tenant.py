"""WS-B: GraphRepository tenant scoping + backfill compatibility.

graph_versions gained a ``tenant_id`` column (migration 007, DEFAULT 'default').
The repository threads an explicit ``tenant_id`` through get/list/list_versions/
save; a foreign-tenant graph is invisible (None), and backfilled 'default' rows
stay readable by a 'default'-tenant caller.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import requires_docker
from tests.graph.test_models import build_graph
from zeroth.contracts.graph.repository import GraphRepository


def _graph(graph_id: str, tenant_id: str | None = None):
    g = build_graph().model_copy(update={"graph_id": graph_id})
    if tenant_id is not None:
        g = g.model_copy(update={"tenant_id": tenant_id})
    return g


@pytest.mark.asyncio
async def test_save_stamps_tenant_and_roundtrips(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    saved = await repo.save(_graph("g-a"), tenant_id="tenant-a")
    assert saved.tenant_id == "tenant-a"

    # Dedicated column round-trips through payload AND filters correctly.
    got = await repo.get("g-a", tenant_id="tenant-a")
    assert got is not None
    assert got.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_get_is_invisible_to_foreign_tenant(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-a"), tenant_id="tenant-a")

    assert await repo.get("g-a", tenant_id="tenant-b") is None
    # Omitting a tenant is the reserved default scope, never a cross-tenant read.
    assert await repo.get("g-a") is None


@pytest.mark.asyncio
async def test_list_and_list_versions_are_tenant_scoped(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-a"), tenant_id="tenant-a")
    await repo.save(_graph("g-b"), tenant_id="tenant-b")

    a_ids = {g.graph_id for g in await repo.list(tenant_id="tenant-a")}
    assert a_ids == {"g-a"}

    assert await repo.list_versions("g-a", tenant_id="tenant-a")
    assert await repo.list_versions("g-a", tenant_id="tenant-b") == []


@pytest.mark.asyncio
async def test_backfilled_default_row_readable_by_default_caller(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    # A graph saved with no explicit tenant defaults to the 'default' sentinel,
    # mirroring how migration 007 backfills pre-existing rows.
    await repo.save(_graph("g-legacy"))  # graph.tenant_id == "default"

    got = await repo.get("g-legacy", tenant_id="default")
    assert got is not None
    assert got.tenant_id == "default"
    # And it is NOT leaked to a non-default tenant.
    assert await repo.get("g-legacy", tenant_id="tenant-a") is None


@pytest.mark.asyncio
async def test_save_stamps_workspace_and_persists_matching_column(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)

    saved = await repo.save(_graph("g-workspace"), tenant_id="tenant-a", workspace_id="workspace-a")

    assert saved.workspace_id == "workspace-a"
    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT workspace_id, payload FROM graph_versions WHERE graph_id = ?",
            ("g-workspace",),
        )
    assert row is not None
    assert row["workspace_id"] == "workspace-a"
    assert '"workspace_id":"workspace-a"' in row["payload"]


@pytest.mark.asyncio
async def test_get_and_list_require_exact_workspace_scope(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-a"), tenant_id="tenant-a", workspace_id="workspace-a")
    await repo.save(_graph("g-b"), tenant_id="tenant-a", workspace_id="workspace-b")

    assert await repo.get("g-a", tenant_id="tenant-a", workspace_id="workspace-b") is None
    assert {
        graph.graph_id
        for graph in await repo.list(tenant_id="tenant-a", workspace_id="workspace-a")
    } == {"g-a"}
    assert {
        graph.graph_id
        for graph in await repo.list(tenant_id="tenant-a", workspace_id="workspace-b")
    } == {"g-b"}


@pytest.mark.asyncio
async def test_lifecycle_update_cannot_cross_workspace(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-a"), tenant_id="tenant-a", workspace_id="workspace-a")

    with pytest.raises(KeyError):
        await repo.archive("g-a", tenant_id="tenant-a", workspace_id="workspace-b")

    graph = await repo.get("g-a", tenant_id="tenant-a", workspace_id="workspace-a")
    assert graph is not None
    assert graph.status.value == "draft"


@pytest.mark.asyncio
async def test_null_workspace_is_exact_scope_not_wildcard(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-legacy"), tenant_id="tenant-a", workspace_id=None)
    await repo.save(_graph("g-scoped"), tenant_id="tenant-a", workspace_id="workspace-a")

    legacy = await repo.get("g-legacy", tenant_id="tenant-a", workspace_id=None)
    assert legacy is not None
    assert legacy.workspace_id is None
    assert await repo.get("g-legacy", tenant_id="tenant-a", workspace_id="workspace-a") is None
    assert {
        graph.graph_id for graph in await repo.list(tenant_id="tenant-a", workspace_id=None)
    } == {"g-legacy"}


@pytest.mark.asyncio
async def test_legacy_payload_without_workspace_is_only_visible_in_null_scope(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    await repo.save(_graph("g-legacy"), tenant_id="tenant-a", workspace_id=None)
    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT payload FROM graph_versions WHERE graph_id = ?", ("g-legacy",)
        )
        assert row is not None
        payload = json.loads(row["payload"])
        payload.pop("workspace_id")
        await connection.execute(
            "UPDATE graph_versions SET payload = ? WHERE graph_id = ?",
            (json.dumps(payload), "g-legacy"),
        )

    graph = await repo.get("g-legacy", tenant_id="tenant-a", workspace_id=None)
    assert graph is not None
    assert graph.workspace_id is None
    assert await repo.get("g-legacy", tenant_id="tenant-a", workspace_id="workspace-a") is None


@pytest.mark.asyncio
async def test_unscoped_save_cannot_transfer_graph_ownership(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    saved = await repo.save(_graph("g-repair"), tenant_id="tenant-a", workspace_id="workspace-a")

    with pytest.raises(KeyError):
        await repo.save(saved.model_copy(update={"tenant_id": "tenant-b", "workspace_id": None}))

    assert await repo.get("g-repair", tenant_id="tenant-a", workspace_id="workspace-a") is not None
    assert await repo.get("g-repair", tenant_id="tenant-b", workspace_id=None) is None


@requires_docker
async def test_workspace_scoped_update_works_on_postgres(postgres_database) -> None:
    repo = GraphRepository(postgres_database)
    saved = await repo.save(
        _graph("g-postgres-workspace"),
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    updated = await repo.save(
        saved.model_copy(update={"name": "Updated"}),
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    assert updated.name == "Updated"
    assert (
        await repo.get(
            updated.graph_id,
            tenant_id="tenant-a",
            workspace_id="workspace-b",
        )
        is None
    )

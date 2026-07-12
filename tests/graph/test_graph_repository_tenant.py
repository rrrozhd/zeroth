"""WS-B: GraphRepository tenant scoping + backfill compatibility.

graph_versions gained a ``tenant_id`` column (migration 007, DEFAULT 'default').
The repository threads an explicit ``tenant_id`` through get/list/list_versions/
save; a foreign-tenant graph is invisible (None), and backfilled 'default' rows
stay readable by a 'default'-tenant caller.
"""

from __future__ import annotations

import pytest

from tests.graph.test_models import build_graph
from zeroth.core.graph.repository import GraphRepository


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
    # No tenant filter (internal path) still finds it.
    assert await repo.get("g-a") is not None


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

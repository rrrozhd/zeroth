"""Durable templates remain tenant/workspace scoped across registry instances."""

from __future__ import annotations

import pytest

from zeroth.contracts.templates.errors import (
    TemplateNotFoundError,
    TemplateVersionExistsError,
)
from zeroth.service.templates import DatabaseTemplateRegistry


@pytest.mark.asyncio
async def test_template_persists_across_registry_instances(sqlite_db) -> None:
    first = DatabaseTemplateRegistry(
        sqlite_db, tenant_id="tenant-a", workspace_id="workspace-a"
    )
    created = await first.register(
        "grounded-answer",
        1,
        "Answer {{ input.question }}",
        metadata={"description": "Grounded answer prompt"},
    )
    second = DatabaseTemplateRegistry(
        sqlite_db, tenant_id="tenant-a", workspace_id="workspace-a"
    )

    restored = await second.get("grounded-answer", 1)

    assert restored == created
    assert restored.metadata["description"] == "Grounded answer prompt"
    assert [item.name for item in await second.list()] == ["grounded-answer"]


@pytest.mark.asyncio
async def test_template_scope_and_duplicate_are_fail_closed(sqlite_db) -> None:
    owner = DatabaseTemplateRegistry(sqlite_db, tenant_id="tenant-a", workspace_id=None)
    foreign = DatabaseTemplateRegistry(sqlite_db, tenant_id="tenant-b", workspace_id=None)
    await owner.register("colliding-name", 1, "Owner {{ value }}")
    await foreign.register("colliding-name", 1, "Foreign {{ value }}")

    assert (await owner.get("colliding-name")).template_str.startswith("Owner")
    assert (await foreign.get("colliding-name")).template_str.startswith("Foreign")
    with pytest.raises(TemplateVersionExistsError):
        await owner.register("colliding-name", 1, "Duplicate")
    with pytest.raises(TemplateNotFoundError):
        await DatabaseTemplateRegistry(
            sqlite_db, tenant_id="tenant-a", workspace_id="other"
        ).get("colliding-name")


@pytest.mark.asyncio
async def test_template_delete_is_version_specific(sqlite_db) -> None:
    registry = DatabaseTemplateRegistry(sqlite_db, tenant_id="tenant-a", workspace_id=None)
    await registry.register("versioned", 1, "one")
    await registry.register("versioned", 2, "two")

    await registry.delete("versioned", 1)

    assert (await registry.get("versioned")).version == 2
    with pytest.raises(TemplateNotFoundError):
        await registry.get("versioned", 1)

from __future__ import annotations

import pytest

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    CrossTenantMaintenanceScopeContext,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
    TenantWideScopeContext,
)


async def test_bound_transaction_accepts_same_structural_scope(async_database) -> None:
    scope = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    runs = ScopedTable(async_database, SERVICE_SCOPE_REGISTRY, "service.runs", scope)
    threads = ScopedTable(async_database, SERVICE_SCOPE_REGISTRY, "service.threads", scope)

    async with runs.transaction() as transaction:
        rebound = transaction.bind(threads)
        assert await rebound.select(where={"thread_id": "unknown"}) == []


@pytest.mark.parametrize(
    "foreign_scope",
    [
        ScopeContext(tenant_id="tenant-b", workspace_id="workspace-a"),
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-b"),
    ],
    ids=["tenant", "workspace"],
)
async def test_bound_transaction_rejects_foreign_workspace_scope(
    async_database,
    foreign_scope: ScopeContext,
) -> None:
    owner = ScopedTable(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.runs",
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    foreign = ScopedTable(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.threads",
        foreign_scope,
    )

    async with owner.transaction() as transaction:
        with pytest.raises(ValueError, match="same structural scope"):
            transaction.bind(foreign)


async def test_bound_transaction_rejects_different_authority_modes(async_database) -> None:
    ordinary = ScopedTable(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.retention_policies",
        NullWorkspaceScopeContext(tenant_id="tenant-a"),
    )
    privileged = ScopedTable.for_privileged_tenant_wide(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.retention_policies",
        TenantWideScopeContext(tenant_id="tenant-a"),
    )
    maintenance = ScopedTable.for_cross_tenant_maintenance(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.retention_policies",
        CrossTenantMaintenanceScopeContext.for_scheduled_maintenance(),
    )

    async with ordinary.transaction() as transaction:
        with pytest.raises(ValueError, match="same structural scope"):
            transaction.bind(privileged)
        with pytest.raises(ValueError, match="same structural scope"):
            transaction.bind(maintenance)


async def test_bound_transaction_accepts_same_privileged_scope(async_database) -> None:
    scope = TenantWideScopeContext(tenant_id="tenant-a")
    policies = ScopedTable.for_privileged_tenant_wide(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.retention_policies",
        scope,
    )
    audit_logs = ScopedTable.for_privileged_tenant_wide(
        async_database,
        SERVICE_SCOPE_REGISTRY,
        "service.retention_audit_log",
        scope,
    )

    async with policies.transaction() as transaction:
        rebound = transaction.bind(audit_logs)
        assert await rebound.select(where={"log_id": "unknown"}) == []


async def test_workspace_scope_is_derived_and_cannot_be_forged(async_database) -> None:
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    table = ScopedTable(async_database, SERVICE_SCOPE_REGISTRY, "service.runs", context)

    with pytest.raises(ValueError, match="workspace_scope does not match"):
        await table.insert({"run_id": "forged", "workspace_scope": "value:workspace-b"})

    with pytest.raises(ValueError, match="ownership columns"):
        await table.update(
            {"workspace_scope": "value:workspace-b"},
            where={"run_id": "unknown"},
        )

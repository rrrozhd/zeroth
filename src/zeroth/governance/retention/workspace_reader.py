"""Read-only owner and workspace discovery for retention scheduling."""

from __future__ import annotations

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    CrossTenantMaintenanceScopeContext,
    ScopedTable,
    TenantWideScopeContext,
)
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    persistence_operation,
    persistence_surface,
)


@persistence_surface("service.runs")
class RetentionOwnerMaintenanceReader:
    """Discover tenants that own retention-managed database rows."""

    def __init__(self, database: AsyncDatabase) -> None:
        context = CrossTenantMaintenanceScopeContext.for_scheduled_maintenance()
        self._runs = ScopedTable.for_cross_tenant_maintenance(
            database, SERVICE_SCOPE_REGISTRY, "service.runs", context
        )
        self._audits = ScopedTable.for_cross_tenant_maintenance(
            database, SERVICE_SCOPE_REGISTRY, "service.node_audits", context
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_tenant_ids(self) -> list[str]:
        tenant_ids: set[str] = set()
        for table in (self._runs, self._audits):
            async with table.transaction() as rows:
                tenant_ids.update(
                    str(row["tenant_id"]) for row in await rows.select(columns=("tenant_id",))
                )
        return sorted(tenant_ids)


@persistence_surface("service.runs")
class RetentionWorkspaceMaintenanceReader:
    """Discover named workspaces inside one exact tenant."""

    def __init__(self, database: AsyncDatabase, tenant_id: str) -> None:
        context = (
            TenantWideScopeContext.for_default_compatibility()
            if tenant_id == "default"
            else TenantWideScopeContext(tenant_id=tenant_id)
        )
        self._runs = ScopedTable.for_privileged_tenant_wide(
            database, SERVICE_SCOPE_REGISTRY, "service.runs", context
        )
        self._audits = ScopedTable.for_privileged_tenant_wide(
            database, SERVICE_SCOPE_REGISTRY, "service.node_audits", context
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_workspace_ids(self) -> list[str]:
        workspace_ids: set[str] = set()
        for table in (self._runs, self._audits):
            async with table.transaction() as rows:
                for row in await rows.select(columns=("workspace_id",)):
                    if row["workspace_id"] is not None:
                        workspace_ids.add(str(row["workspace_id"]))
        return sorted(workspace_ids)

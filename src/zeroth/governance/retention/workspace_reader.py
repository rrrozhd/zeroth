"""Read-only workspace discovery for retention scheduling."""

from __future__ import annotations

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    ScopedTable,
    TenantWideScopeContext,
)


class RetentionWorkspaceMaintenanceReader:
    """Discover named workspaces without exposing a mutation surface."""

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

    async def list_workspace_ids(self) -> list[str]:
        workspace_ids: set[str] = set()
        for table in (self._runs, self._audits):
            async with table.transaction() as rows:
                for row in await rows.select(columns=("workspace_id",)):
                    workspace_id = row["workspace_id"]
                    if workspace_id is not None:
                        workspace_ids.add(str(workspace_id))
        return sorted(workspace_ids)

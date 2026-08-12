"""Database-backed coordination for tenant retention administration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


@dataclass(frozen=True, slots=True)
class RetentionTransaction:
    """A database transaction whose retention tenant cannot be reassigned."""

    connection: BoundStructuredTable
    tenant_id: str


@persistence_surface(
    "service.retention_coordination", probe=named_isolation_probe("_drive_coordination")
)
class RetentionCoordinator:
    """Serialize retention decisions and legal-hold changes per tenant."""

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext,
    ) -> None:
        self._database = database
        self._scope_context = scope_context
        self._coordination = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.retention_coordination",
            scope_context,
        )

    @property
    def tenant_id(self) -> str:
        """Tenant structurally bound to every coordinated transaction."""
        return self._scope_context.tenant_id

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.READ)
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[RetentionTransaction]:
        """Yield a write transaction holding the tenant coordination row."""
        async with self._coordination.transaction(write_lock=True) as coordination:
            await coordination.insert_if_absent(
                {"updated_at": "1970-01-01T00:00:00+00:00"},
                conflict_columns=("tenant_id",),
            )
            row = await coordination.select_one(where={}, for_update=True)
            if row is None:  # pragma: no cover - INSERT + SELECT is invariant
                raise RuntimeError("failed to initialize retention lock")
            yield RetentionTransaction(
                connection=coordination, tenant_id=self._scope_context.tenant_id
            )

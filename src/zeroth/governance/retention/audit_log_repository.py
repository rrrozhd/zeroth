"""Append-only retention audit log (WS-E).

Records every erasure/hold step so a compliance reviewer can prove what was
purged, when, and under which reason (ttl | rte | manual) — the paper trail that
sits beside the immutable node-audit chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncConnection,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import to_json_value
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


@persistence_surface(
    "service.retention_audit_log", probe=named_isolation_probe("_drive_retention_audit")
)
class RetentionAuditLogRepository:
    """Writes and reads ``retention_audit_log`` entries."""

    def __init__(self, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext) -> None:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._database = database
        self._scope_context = scope_context
        self._logs = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.retention_audit_log", scope_context
        )

    @classmethod
    def for_default_compatibility(cls, database: AsyncDatabase) -> RetentionAuditLogRepository:
        return cls(database, NullWorkspaceScopeContext.for_default_compatibility())

    @property
    def tenant_id(self) -> str:
        """Tenant structurally bound to this audit log."""
        return self._scope_context.tenant_id

    @persistence_operation(ResourceOperation.CREATE)
    async def record(
        self,
        *,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one log entry; returns its id."""
        log_id = uuid4().hex
        await self._logs.insert(
            {
                "log_id": log_id,
                "run_id": run_id,
                "action": action,
                "reason": reason,
                "detail": to_json_value(dict(detail)) if detail is not None else None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return log_id

    @persistence_operation(ResourceOperation.CREATE)
    async def record_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one log entry through an existing transaction."""
        log_id = uuid4().hex
        await self._logs.in_transaction(connection).insert(
            {
                "log_id": log_id,
                "run_id": run_id,
                "action": action,
                "reason": reason,
                "detail": to_json_value(dict(detail)) if detail is not None else None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return log_id

    @persistence_operation(ResourceOperation.READ)
    async def get(self, log_id: str) -> dict[str, Any] | None:
        """Load one retention log row by id."""
        row = await self._logs.select_one(where={"log_id": log_id})
        return None if row is None else dict(row)

    @persistence_operation(ResourceOperation.READ)
    async def get_in_transaction(
        self,
        connection: AsyncConnection,
        log_id: str,
    ) -> dict[str, Any] | None:
        """Load one retention log through the caller's transaction."""
        row = await self._logs.in_transaction(connection).select_one(where={"log_id": log_id})
        return None if row is None else dict(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_for_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """List a run's log rows through the caller's transaction."""
        rows = await self._logs.in_transaction(connection).select(
            where={"run_id": run_id}, order_by=("created_at", "log_id")
        )
        return [dict(row) for row in rows]

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_for_tenant(self) -> list[dict[str, Any]]:
        """Return raw log rows for a tenant, oldest first."""
        async with self._logs.transaction() as logs:
            rows = await logs.select(order_by=("created_at", "log_id"))
        return [dict(row) for row in rows]

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return raw log rows for a single run, oldest first."""
        async with self._logs.transaction() as logs:
            rows = await logs.select(where={"run_id": run_id}, order_by=("created_at", "log_id"))
        return [dict(row) for row in rows]

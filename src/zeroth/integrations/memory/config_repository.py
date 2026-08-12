"""Persistence for runtime-managed memory connector configurations.

Operators create connectors through the console (POST /v1/connectors); the
configs are stored here so they survive restarts and are re-registered at
bootstrap by ``load_persisted_connectors``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class MemoryConnectorConfig(BaseModel):
    """A persisted runtime connector configuration row."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    backend_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    # WS-B: owning tenant. Connector refs carry DSNs/credentials in ``params``,
    # so a config registered by tenant A must be invisible to tenant B when
    # both deployments share the physical control-plane DB. Defaults to the
    # reserved single-tenant sentinel.
    tenant_id: str = "default"
    created_at: str
    updated_at: str


@persistence_surface(
    "service.memory_connector_configs", probe=named_isolation_probe("_drive_memory_configs")
)
class MemoryConnectorConfigRepository:
    """Raw-SQL repository over the ``memory_connector_configs`` table.

    WS-B: reads (``get``/``list``) and ``delete`` are tenant-scoped, and
    ``upsert`` stamps the owning tenant. Read isolation is the goal — a
    tenant only sees/deletes its own DSN-bearing rows. NOTE: ``ref`` remains
    the table PRIMARY KEY (migration 007 is ADD COLUMN only, no PK rebuild),
    so two tenants cannot persist the *same* ref on one shared DB (the second
    INSERT raises an IntegrityError). That is an accepted limitation of the
    single-tenant-per-deployment model, where refs are namespaced per
    deployment in practice.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    def _configs(self, tenant_id: str | None) -> ScopedTable:
        context = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if tenant_id in {None, "default"}
            else NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.memory_connector_configs",
            context,
        )

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def upsert(
        self,
        ref: str,
        backend_type: str,
        params: dict[str, Any],
        *,
        tenant_id: str = "default",
    ) -> MemoryConnectorConfig:
        """Insert or update a connector config; returns the stored row."""
        now = _utcnow_iso()
        params_json = json.dumps(params, sort_keys=True, separators=(",", ":"))
        configs = self._configs(tenant_id)
        async with configs.transaction(write_lock=True) as table:
            stored = await table.upsert(
                {
                    "ref": ref,
                    "backend_type": backend_type,
                    "params": params_json,
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=("ref",),
                update_columns=("backend_type", "params", "updated_at"),
                returning="ref",
                update_where={"tenant_id": tenant_id},
            )
        if not stored:
            raise KeyError(ref)
        return await self.get(ref, tenant_id=tenant_id)  # type: ignore[return-value]

    @persistence_operation(ResourceOperation.READ)
    async def get(self, ref: str, *, tenant_id: str | None = None) -> MemoryConnectorConfig | None:
        """Load a single connector config by ref (optionally tenant-scoped)."""
        row = await self._configs(tenant_id).select_one(where={"ref": ref})
        if row is None:
            return None
        return self._row_to_config(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(self, *, tenant_id: str | None = None) -> list[MemoryConnectorConfig]:
        """Persisted connector configs, ordered by ref (optionally tenant-scoped)."""
        async with self._configs(tenant_id).transaction() as table:
            rows = await table.select(order_by=("ref",))
        return [self._row_to_config(row) for row in rows]

    @persistence_operation(ResourceOperation.DELETE)
    async def delete(self, ref: str, *, tenant_id: str | None = None) -> bool:
        """Delete a connector config. Returns True if a row existed (in tenant)."""
        async with self._configs(tenant_id).transaction(write_lock=True) as table:
            existing = await table.select_one(where={"ref": ref}, columns=("ref",))
            if existing is None:
                return False
            await table.delete(where={"ref": ref})
        return True

    def _row_to_config(self, row: Any) -> MemoryConnectorConfig:
        raw_params = row["params"]
        if isinstance(raw_params, bytes):
            raw_params = raw_params.decode("utf-8")
        return MemoryConnectorConfig(
            ref=row["ref"],
            backend_type=row["backend_type"],
            params=json.loads(raw_params) if raw_params else {},
            tenant_id=row["tenant_id"] or "default",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

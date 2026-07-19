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

from zeroth.platform.storage import AsyncDatabase


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
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                "SELECT ref FROM memory_connector_configs WHERE ref = ? AND tenant_id = ?",
                (ref, tenant_id),
            )
            if existing is None:
                await connection.execute(
                    """
                    INSERT INTO memory_connector_configs (
                        ref, backend_type, params, tenant_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ref, backend_type, params_json, tenant_id, now, now),
                )
            else:
                await connection.execute(
                    """
                    UPDATE memory_connector_configs
                    SET backend_type = ?, params = ?, updated_at = ?
                    WHERE ref = ? AND tenant_id = ?
                    """,
                    (backend_type, params_json, now, ref, tenant_id),
                )
        return await self.get(ref, tenant_id=tenant_id)  # type: ignore[return-value]

    async def get(self, ref: str, *, tenant_id: str | None = None) -> MemoryConnectorConfig | None:
        """Load a single connector config by ref (optionally tenant-scoped)."""
        sql = "SELECT * FROM memory_connector_configs WHERE ref = ?"
        params: tuple[object, ...] = (ref,)
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params = (ref, tenant_id)
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(sql, params)
        if row is None:
            return None
        return self._row_to_config(row)

    async def list(self, *, tenant_id: str | None = None) -> list[MemoryConnectorConfig]:
        """Persisted connector configs, ordered by ref (optionally tenant-scoped)."""
        sql = "SELECT * FROM memory_connector_configs"
        params: tuple[object, ...] = ()
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params = (tenant_id,)
        sql += " ORDER BY ref"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        return [self._row_to_config(row) for row in rows]

    async def delete(self, ref: str, *, tenant_id: str | None = None) -> bool:
        """Delete a connector config. Returns True if a row existed (in tenant)."""
        select_sql = "SELECT ref FROM memory_connector_configs WHERE ref = ?"
        delete_sql = "DELETE FROM memory_connector_configs WHERE ref = ?"
        params: tuple[object, ...] = (ref,)
        if tenant_id is not None:
            select_sql += " AND tenant_id = ?"
            delete_sql += " AND tenant_id = ?"
            params = (ref, tenant_id)
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(select_sql, params)
            if existing is None:
                return False
            await connection.execute(delete_sql, params)
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

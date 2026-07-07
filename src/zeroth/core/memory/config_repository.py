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

from zeroth.core.storage import AsyncDatabase


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class MemoryConnectorConfig(BaseModel):
    """A persisted runtime connector configuration row."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    backend_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class MemoryConnectorConfigRepository:
    """Raw-SQL repository over the ``memory_connector_configs`` table."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    async def upsert(
        self,
        ref: str,
        backend_type: str,
        params: dict[str, Any],
    ) -> MemoryConnectorConfig:
        """Insert or update a connector config; returns the stored row."""
        now = _utcnow_iso()
        params_json = json.dumps(params, sort_keys=True, separators=(",", ":"))
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                "SELECT ref FROM memory_connector_configs WHERE ref = ?",
                (ref,),
            )
            if existing is None:
                await connection.execute(
                    """
                    INSERT INTO memory_connector_configs (
                        ref, backend_type, params, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (ref, backend_type, params_json, now, now),
                )
            else:
                await connection.execute(
                    """
                    UPDATE memory_connector_configs
                    SET backend_type = ?, params = ?, updated_at = ?
                    WHERE ref = ?
                    """,
                    (backend_type, params_json, now, ref),
                )
        return await self.get(ref)  # type: ignore[return-value]

    async def get(self, ref: str) -> MemoryConnectorConfig | None:
        """Load a single connector config by ref."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT * FROM memory_connector_configs WHERE ref = ?",
                (ref,),
            )
        if row is None:
            return None
        return self._row_to_config(row)

    async def list(self) -> list[MemoryConnectorConfig]:
        """All persisted connector configs, ordered by ref."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM memory_connector_configs ORDER BY ref",
                (),
            )
        return [self._row_to_config(row) for row in rows]

    async def delete(self, ref: str) -> bool:
        """Delete a connector config. Returns True if a row existed."""
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                "SELECT ref FROM memory_connector_configs WHERE ref = ?",
                (ref,),
            )
            if existing is None:
                return False
            await connection.execute(
                "DELETE FROM memory_connector_configs WHERE ref = ?",
                (ref,),
            )
        return True

    def _row_to_config(self, row: Any) -> MemoryConnectorConfig:
        raw_params = row["params"]
        if isinstance(raw_params, bytes):
            raw_params = raw_params.decode("utf-8")
        return MemoryConnectorConfig(
            ref=row["ref"],
            backend_type=row["backend_type"],
            params=json.loads(raw_params) if raw_params else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

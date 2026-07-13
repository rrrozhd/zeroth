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

from zeroth.core.storage import AsyncConnection, AsyncDatabase
from zeroth.core.storage.json import to_json_value


class RetentionAuditLogRepository:
    """Writes and reads ``retention_audit_log`` entries."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def record(
        self,
        *,
        tenant_id: str,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one log entry; returns its id."""
        async with self._database.transaction() as connection:
            return await self.record_in_transaction(
                connection,
                tenant_id=tenant_id,
                action=action,
                run_id=run_id,
                reason=reason,
                detail=detail,
            )

    async def record_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        tenant_id: str,
        action: str,
        run_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one log entry through an existing transaction."""
        log_id = uuid4().hex
        await connection.execute(
            """
            INSERT INTO retention_audit_log
                (log_id, tenant_id, run_id, action, reason, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                tenant_id,
                run_id,
                action,
                reason,
                to_json_value(dict(detail)) if detail is not None else None,
                datetime.now(UTC).isoformat(),
            ),
        )
        return log_id

    async def get(self, log_id: str) -> dict[str, Any] | None:
        """Load one retention log row by id."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT * FROM retention_audit_log WHERE log_id = ?",
                (log_id,),
            )
        return None if row is None else dict(row)

    async def list_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return raw log rows for a tenant, oldest first."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM retention_audit_log WHERE tenant_id = ? ORDER BY created_at, log_id",
                (tenant_id,),
            )
        return [dict(row) for row in rows]

    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return raw log rows for a single run, oldest first."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM retention_audit_log WHERE run_id = ? ORDER BY created_at, log_id",
                (run_id,),
            )
        return [dict(row) for row in rows]

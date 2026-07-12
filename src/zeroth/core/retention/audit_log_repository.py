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

from zeroth.core.storage import AsyncDatabase
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
        log_id = uuid4().hex
        async with self._database.transaction() as connection:
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

    async def list_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return raw log rows for a tenant, oldest first."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM retention_audit_log WHERE tenant_id = ? "
                "ORDER BY created_at, log_id",
                (tenant_id,),
            )
        return [dict(row) for row in rows]

    async def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return raw log rows for a single run, oldest first."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM retention_audit_log WHERE run_id = ? "
                "ORDER BY created_at, log_id",
                (run_id,),
            )
        return [dict(row) for row in rows]

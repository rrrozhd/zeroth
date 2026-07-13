"""Dialect-portable helpers for database-backed coordination rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from zeroth.core.storage.database import AsyncConnection

_COORDINATION_KEYS = {
    ("audit_chain_heads", "run_id"),
    ("retention_coordination", "tenant_id"),
}


async def ensure_and_lock_row(
    connection: AsyncConnection,
    *,
    backend: Literal["sqlite", "postgres"],
    table: str,
    key_column: str,
    key: str,
) -> dict[str, object] | None:
    """Create one approved coordination row and lock it on PostgreSQL.

    Identifiers are selected only after exact allow-list validation; values
    remain bound parameters on both supported backends.
    """
    if (table, key_column) not in _COORDINATION_KEYS:
        raise ValueError(f"unsupported coordination table/key pair: {table}.{key_column}")
    if backend not in {"sqlite", "postgres"}:
        raise ValueError(f"unsupported database backend: {backend}")

    await connection.execute(
        f"INSERT INTO {table} ({key_column}, updated_at) VALUES (?, ?) "
        f"ON CONFLICT({key_column}) DO NOTHING",
        (key, datetime.now(UTC).isoformat()),
    )
    suffix = " FOR UPDATE" if backend == "postgres" else ""
    return await connection.fetch_one(
        f"SELECT * FROM {table} WHERE {key_column} = ?{suffix}",
        (key,),
    )

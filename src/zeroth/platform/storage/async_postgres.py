"""Async Postgres database implementation using psycopg3 with connection pooling.

Implements the AsyncDatabase protocol with an AsyncConnectionPool
and automatic placeholder conversion from SQLite-style ? to psycopg %s.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection as PsycopgAsyncConnection
from psycopg.errors import LockNotAvailable, QueryCanceled
from psycopg_pool import AsyncConnectionPool

from zeroth.platform.storage.database import (
    DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    CoordinationTimeoutError,
    validate_coordination_timeout,
)

_PLACEHOLDER_RE = re.compile(r"\?")


def _sqlite_to_psycopg(sql: str) -> str:
    """Convert SQLite-style ? placeholders to psycopg %s placeholders."""
    return _PLACEHOLDER_RE.sub("%s", sql)


def _is_lock_timeout_error(exc: LockNotAvailable | QueryCanceled) -> bool:
    """Distinguish configured lock timeouts from other PostgreSQL cancellations."""
    diag = exc.diag
    messages = (
        str(exc),
        diag.message_primary,
        diag.message_detail,
        diag.context,
    )
    return "lock timeout" in " ".join(message for message in messages if message).casefold()


class PostgresConnection:
    """AsyncConnection implementation wrapping a psycopg AsyncConnection."""

    def __init__(self, conn: PsycopgAsyncConnection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a SQL statement without returning results."""
        converted = _sqlite_to_psycopg(sql)
        await self._conn.execute(converted, params or None)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        converted = _sqlite_to_psycopg(sql)
        cursor = await self._conn.execute(converted, params or None)
        row = await cursor.fetchone()
        if row is None:
            return None
        col_names = [desc.name for desc in cursor.description]
        return dict(zip(col_names, row, strict=True))

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts."""
        converted = _sqlite_to_psycopg(sql)
        cursor = await self._conn.execute(converted, params or None)
        rows = await cursor.fetchall()
        if not rows:
            return []
        col_names = [desc.name for desc in cursor.description]
        return [dict(zip(col_names, row, strict=True)) for row in rows]

    async def execute_script(self, sql: str) -> None:
        """Execute a multi-statement SQL script by splitting on semicolons."""
        for statement in sql.split(";"):
            stripped = statement.strip()
            if stripped:
                await self._conn.execute(stripped)


class AsyncPostgresDatabase:
    """AsyncDatabase implementation backed by a psycopg AsyncConnectionPool.

    Use the create() classmethod to construct an instance with an opened pool.
    """

    backend = "postgres"

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    ) -> None:
        self._pool = pool
        self.coordination_timeout_seconds = validate_coordination_timeout(
            coordination_timeout_seconds
        )

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    ) -> AsyncPostgresDatabase:
        """Create and open a connection pool, returning an AsyncPostgresDatabase."""
        coordination_timeout_seconds = validate_coordination_timeout(coordination_timeout_seconds)
        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        return cls(
            pool,
            coordination_timeout_seconds=coordination_timeout_seconds,
        )

    @asynccontextmanager
    async def transaction(self, *, write_lock: bool = False) -> AsyncIterator[PostgresConnection]:
        """Acquire a connection from the pool, run inside a transaction."""
        try:
            async with self._pool.connection() as conn, conn.transaction():
                if write_lock:
                    timeout_ms = max(1, round(self.coordination_timeout_seconds * 1000))
                    await conn.execute(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
                yield PostgresConnection(conn)
        except (LockNotAvailable, QueryCanceled) as exc:
            if write_lock and _is_lock_timeout_error(exc):
                raise CoordinationTimeoutError(
                    "timed out acquiring PostgreSQL coordination lock"
                ) from exc
            raise

    async def close(self) -> None:
        """Close the connection pool."""
        await self._pool.close()

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
from psycopg_pool import AsyncConnectionPool

_PLACEHOLDER_RE = re.compile(r"\?")


def _sqlite_to_psycopg(sql: str) -> str:
    """Convert SQLite-style ? placeholders to psycopg %s placeholders."""
    return _PLACEHOLDER_RE.sub("%s", sql)


class PostgresConnection:
    """AsyncConnection implementation wrapping a psycopg AsyncConnection."""

    def __init__(self, conn: PsycopgAsyncConnection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a SQL statement without returning results."""
        converted = _sqlite_to_psycopg(sql)
        await self._conn.execute(converted, params or None)

    async def fetch_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        converted = _sqlite_to_psycopg(sql)
        cursor = await self._conn.execute(converted, params or None)
        row = await cursor.fetchone()
        if row is None:
            return None
        col_names = [desc.name for desc in cursor.description]
        return dict(zip(col_names, row, strict=True))

    async def fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
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

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
    ) -> AsyncPostgresDatabase:
        """Create and open a connection pool, returning an AsyncPostgresDatabase."""
        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        return cls(pool)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PostgresConnection]:
        """Acquire a connection from the pool, run inside a transaction."""
        async with self._pool.connection() as conn, conn.transaction():
            yield PostgresConnection(conn)

    async def close(self) -> None:
        """Close the connection pool."""
        await self._pool.close()

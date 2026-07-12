"""Async SQLite database implementation using aiosqlite.

Implements the AsyncDatabase protocol with per-transaction connections,
WAL mode, and foreign key enforcement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from zeroth.core.storage.database import (
    DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    CoordinationTimeoutError,
    validate_coordination_timeout,
)
from zeroth.core.storage.sqlite import EncryptedField


def _convert_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an aiosqlite Row to a plain dict."""
    return dict(row)


class AsyncSQLiteConnection:
    """AsyncConnection implementation wrapping an aiosqlite connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a SQL statement without returning results."""
        await self._conn.execute(sql, params)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return _convert_row(row)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts."""
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [_convert_row(row) for row in rows]

    async def execute_script(self, sql: str) -> None:
        """Execute a multi-statement SQL script."""
        await self._conn.executescript(sql)


class AsyncSQLiteDatabase:
    """AsyncDatabase implementation backed by aiosqlite.

    Each call to transaction() opens a fresh connection with recommended
    PRAGMAs (foreign keys, WAL mode, synchronous NORMAL).
    """

    backend = "sqlite"

    def __init__(
        self,
        path: str,
        *,
        encryption_key: str | bytes | None = None,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_TIMEOUT_SECONDS,
    ) -> None:
        self.path = path
        self.coordination_timeout_seconds = validate_coordination_timeout(
            coordination_timeout_seconds
        )
        self.encrypted_field = (
            EncryptedField(encryption_key) if encryption_key is not None else None
        )

    @asynccontextmanager
    async def transaction(
        self, *, write_lock: bool = False
    ) -> AsyncIterator[AsyncSQLiteConnection]:
        """Open a connection, yield it inside a transaction, then commit or rollback."""
        conn = await aiosqlite.connect(
            self.path,
            timeout=self.coordination_timeout_seconds,
        )
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            timeout_ms = max(1, round(self.coordination_timeout_seconds * 1000))
            await conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            await conn.execute("BEGIN IMMEDIATE" if write_lock else "BEGIN")
            yield AsyncSQLiteConnection(conn)
            await conn.commit()
        except sqlite3.OperationalError as exc:
            await conn.rollback()
            if write_lock and ("locked" in str(exc).lower() or "busy" in str(exc).lower()):
                raise CoordinationTimeoutError(
                    "timed out acquiring SQLite coordination lock"
                ) from exc
            raise
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def close(self) -> None:
        """No-op -- connections are per-transaction."""

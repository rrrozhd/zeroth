"""Async SQLite database implementation using aiosqlite.

Implements the AsyncDatabase protocol with per-transaction connections,
WAL mode, and foreign key enforcement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
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


def _split_sql_script(sql: str) -> Iterator[str]:
    """Yield complete SQLite statements without committing the transaction."""
    buffer: list[str] = []
    state = "normal"
    token: list[str] = []
    trigger_prefix: list[str] = []
    trigger_detection_possible = True
    is_trigger = False
    trigger_body_depth = 0
    trigger_case_depth = 0
    trigger_terminal_end = False

    def observe_token() -> None:
        nonlocal is_trigger
        nonlocal trigger_body_depth
        nonlocal trigger_case_depth
        nonlocal trigger_detection_possible
        nonlocal trigger_terminal_end
        if not token:
            return
        keyword = "".join(token).upper()
        token.clear()

        if trigger_detection_possible and not is_trigger:
            if not trigger_prefix:
                if keyword == "CREATE":
                    trigger_prefix.append(keyword)
                else:
                    trigger_detection_possible = False
            elif trigger_prefix == ["CREATE"]:
                if keyword == "TRIGGER":
                    is_trigger = True
                elif keyword in {"TEMP", "TEMPORARY"}:
                    trigger_prefix.append(keyword)
                else:
                    trigger_detection_possible = False
            elif keyword == "TRIGGER":
                is_trigger = True
            else:
                trigger_detection_possible = False
            return

        if not is_trigger:
            return
        if keyword == "CASE":
            trigger_case_depth += 1
        elif keyword == "END":
            if trigger_case_depth:
                trigger_case_depth -= 1
            elif trigger_body_depth:
                trigger_body_depth -= 1
                trigger_terminal_end = trigger_body_depth == 0
        elif keyword == "BEGIN" and not trigger_case_depth:
            trigger_body_depth += 1

    index = 0
    while index < len(sql):
        character = sql[index]
        buffer.append(character)

        if state == "normal":
            if character.isalnum() or character == "_":
                token.append(character)
            else:
                observe_token()
            if character in {"'", '"', "`"}:
                state = character
            elif character == "[":
                state = "]"
            elif character == "-" and index + 1 < len(sql) and sql[index + 1] == "-":
                index += 1
                buffer.append(sql[index])
                state = "line_comment"
            elif character == "/" and index + 1 < len(sql) and sql[index + 1] == "*":
                index += 1
                buffer.append(sql[index])
                state = "block_comment"
            elif character == ";":
                should_check_statement = (
                    not is_trigger or trigger_terminal_end or trigger_body_depth == 0
                )
                if not should_check_statement:
                    index += 1
                    continue
                statement = "".join(buffer)
                if sqlite3.complete_statement(statement):
                    statement = statement.strip()
                    if statement:
                        yield statement
                    buffer.clear()
                    token.clear()
                    trigger_prefix.clear()
                    trigger_detection_possible = True
                    is_trigger = False
                    trigger_body_depth = 0
                    trigger_case_depth = 0
                    trigger_terminal_end = False
        elif state in {"'", '"', "`"} and character == state:
            if index + 1 < len(sql) and sql[index + 1] == state:
                index += 1
                buffer.append(sql[index])
            else:
                state = "normal"
        elif (state == "]" and character == "]") or (
            state == "line_comment" and character in {"\r", "\n"}
        ):
            state = "normal"
        elif (
            state == "block_comment"
            and character == "*"
            and index + 1 < len(sql)
            and sql[index + 1] == "/"
        ):
            index += 1
            buffer.append(sql[index])
            state = "normal"

        index += 1

    trailing_statement = "".join(buffer).strip()
    if trailing_statement:
        yield trailing_statement


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
        for statement in _split_sql_script(sql):
            await self._conn.execute(statement)


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
            if write_lock:
                await conn.execute("BEGIN IMMEDIATE")
            # Leave ordinary transactions lazy: an explicit deferred BEGIN followed
            # by a read can become a stale WAL snapshot and fail its later write with
            # SQLITE_BUSY_SNAPSHOT. aiosqlite begins implicitly on the first DML.
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

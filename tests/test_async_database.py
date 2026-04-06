"""Tests for the async database abstraction layer."""

from __future__ import annotations

import pytest

from zeroth.storage.async_postgres import _sqlite_to_psycopg
from zeroth.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.storage.database import AsyncDatabase


class TestAsyncSQLiteDatabase:
    """Verify AsyncSQLiteDatabase transaction and query behavior."""

    async def test_async_sqlite_transaction_commit(self, tmp_path):
        """Write a row and read it back -- commit should persist the data."""
        db = AsyncSQLiteDatabase(str(tmp_path / "test.db"))
        async with db.transaction() as conn:
            await conn.execute_script(
                "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT)"
            )
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO items (id, name) VALUES (?, ?)", ("1", "alpha"))
        async with db.transaction() as conn:
            row = await conn.fetch_one("SELECT id, name FROM items WHERE id = ?", ("1",))
        assert row is not None
        assert row["id"] == "1"
        assert row["name"] == "alpha"

    async def test_async_sqlite_transaction_rollback(self, tmp_path):
        """An exception inside a transaction should rollback the changes."""
        db = AsyncSQLiteDatabase(str(tmp_path / "test.db"))
        async with db.transaction() as conn:
            await conn.execute_script(
                "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT)"
            )
        with pytest.raises(RuntimeError, match="deliberate"):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO items (id, name) VALUES (?, ?)", ("2", "beta")
                )
                raise RuntimeError("deliberate error")
        async with db.transaction() as conn:
            row = await conn.fetch_one("SELECT id, name FROM items WHERE id = ?", ("2",))
        assert row is None

    async def test_async_sqlite_fetch_one_returns_none_for_missing(self, tmp_path):
        """fetch_one returns None when no row matches, not an error."""
        db = AsyncSQLiteDatabase(str(tmp_path / "test.db"))
        async with db.transaction() as conn:
            await conn.execute_script(
                "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT)"
            )
        async with db.transaction() as conn:
            result = await conn.fetch_one(
                "SELECT id, name FROM items WHERE id = ?", ("nonexistent",)
            )
        assert result is None

    async def test_async_sqlite_fetch_all_returns_empty_list(self, tmp_path):
        """fetch_all returns an empty list when no rows match."""
        db = AsyncSQLiteDatabase(str(tmp_path / "test.db"))
        async with db.transaction() as conn:
            await conn.execute_script(
                "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT)"
            )
        async with db.transaction() as conn:
            results = await conn.fetch_all("SELECT id, name FROM items")
        assert results == []


class TestDatabaseFactory:
    """Verify the create_database factory selects the correct backend."""

    async def test_factory_creates_sqlite_by_default(self, tmp_path):
        """create_database() should return AsyncSQLiteDatabase when backend='sqlite'."""
        from zeroth.config.settings import ZerothSettings
        from zeroth.storage.factory import create_database

        settings = ZerothSettings(database={"backend": "sqlite", "sqlite_path": str(tmp_path / "f.db")})
        db = await create_database(settings)
        assert isinstance(db, AsyncSQLiteDatabase)


class TestPlaceholderConversion:
    """Verify the ? to %s SQL placeholder conversion."""

    def test_placeholder_conversion(self):
        result = _sqlite_to_psycopg("SELECT * WHERE id = ? AND name = ?")
        assert result == "SELECT * WHERE id = %s AND name = %s"

    def test_no_placeholders_unchanged(self):
        result = _sqlite_to_psycopg("SELECT * FROM items")
        assert result == "SELECT * FROM items"


class TestProtocolCompliance:
    """Verify that implementations satisfy the protocol at runtime."""

    def test_async_sqlite_is_async_database(self):
        db = AsyncSQLiteDatabase("/tmp/proto_test.db")
        assert isinstance(db, AsyncDatabase)

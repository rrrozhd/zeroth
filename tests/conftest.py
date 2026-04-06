from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.service.bootstrap import run_migrations
from zeroth.storage.async_sqlite import AsyncSQLiteDatabase


@pytest.fixture
async def async_database(tmp_path: Path) -> AsyncSQLiteDatabase:
    """Async SQLite database for tests. Runs Alembic migrations on a temp DB."""
    db_path = str(tmp_path / "zeroth.db")
    run_migrations(f"sqlite:///{db_path}")
    db = AsyncSQLiteDatabase(path=db_path)
    yield db
    await db.close()


# Alias so every test that used the old `sqlite_db` fixture works with the
# async database after the Plan-02 repository rewrite.
@pytest.fixture
async def sqlite_db(async_database: AsyncSQLiteDatabase) -> AsyncSQLiteDatabase:
    return async_database

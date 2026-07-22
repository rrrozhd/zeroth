from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase


@pytest.fixture
async def runs_db(tmp_path: Path) -> AsyncSQLiteDatabase:
    db_path = str(tmp_path / "runs.db")
    run_migrations(f"sqlite:///{db_path}")
    db = AsyncSQLiteDatabase(path=db_path)
    yield db
    await db.close()

"""Storage-specific fixtures that provide the legacy sync SQLiteDatabase."""
from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.storage import SQLiteDatabase


@pytest.fixture
def sqlite_db(tmp_path: Path) -> SQLiteDatabase:
    """Legacy sync SQLiteDatabase for storage-layer-specific tests."""
    return SQLiteDatabase(tmp_path / "zeroth.db")

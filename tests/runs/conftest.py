from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.storage import SQLiteDatabase


@pytest.fixture
def runs_db(tmp_path: Path) -> SQLiteDatabase:
    return SQLiteDatabase(tmp_path / "runs.db")

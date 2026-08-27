from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from zeroth.econ.plane import main
from zeroth.econ.plane.common import bootstrap as common_bootstrap
from zeroth.platform.storage.schema_revision import SchemaRevision, read_schema_revision


def test_econ_revision_reader_classifies_current_behind_and_unknown(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'econ.db'}")

    assert read_schema_revision(engine, "zeroth.econ.plane._migrations").model_dump() == {
        "applied": None,
        "head": "20260824_10",
        "state": "unknown",
    }

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260811_05')"))
    assert read_schema_revision(engine, "zeroth.econ.plane._migrations").model_dump() == {
        "applied": "20260811_05",
        "head": "20260824_10",
        "state": "behind",
    }

    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '20260822_08'"))
    assert read_schema_revision(engine, "zeroth.econ.plane._migrations").model_dump() == {
        "applied": "20260822_08",
        "head": "20260824_10",
        "state": "behind",
    }

    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '20260823_09'"))
    assert read_schema_revision(engine, "zeroth.econ.plane._migrations").model_dump() == {
        "applied": "20260823_09",
        "head": "20260824_10",
        "state": "behind",
    }

    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '20260824_10'"))
    assert read_schema_revision(engine, "zeroth.econ.plane._migrations").model_dump() == {
        "applied": "20260824_10",
        "head": "20260824_10",
        "state": "current",
    }


@pytest.mark.parametrize(
    ("revision", "status"),
    [
        (SchemaRevision(applied="20260812_07", head="20260812_07", state="current"), "ok"),
        (SchemaRevision(applied="20260812_04", head="20260812_07", state="behind"), "degraded"),
        (SchemaRevision(applied=None, head="20260812_07", state="unknown"), "degraded"),
    ],
)
def test_econ_health_uses_captured_schema_revision(
    monkeypatch: pytest.MonkeyPatch,
    revision: SchemaRevision,
    status: str,
) -> None:
    monkeypatch.setattr(common_bootstrap, "_schema_revision", revision)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": status,
        "schema_revision": revision.model_dump(),
    }

from __future__ import annotations

import pytest

from zeroth.storage import Migration, SQLiteDatabase


def test_apply_migrations_updates_schema_version(sqlite_db: SQLiteDatabase) -> None:
    applied = sqlite_db.apply_migrations(
        "contracts",
        [
            Migration(
                version=1,
                name="create_contract_table",
                sql="""
                CREATE TABLE contract_models (
                    model_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """,
            ),
            Migration(
                version=2,
                name="add_model_version",
                sql="""
                ALTER TABLE contract_models
                ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
                """,
            ),
        ],
    )

    with sqlite_db.transaction() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(contract_models)").fetchall()
        }

    assert [migration.version for migration in applied] == [1, 2]
    assert sqlite_db.fetch_schema_version("contracts") == 2
    assert columns == {"model_id", "payload", "version"}


def test_apply_migrations_is_idempotent(sqlite_db: SQLiteDatabase) -> None:
    migrations = [
        Migration(
            version=1,
            name="create_contract_table",
            sql="""
            CREATE TABLE contract_models (
                model_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """,
        )
    ]

    first_run = sqlite_db.apply_migrations("contracts", migrations)
    second_run = sqlite_db.apply_migrations("contracts", migrations)

    assert len(first_run) == 1
    assert second_run == []


def test_apply_migrations_requires_contiguous_versions(sqlite_db: SQLiteDatabase) -> None:
    with pytest.raises(ValueError, match="contiguous"):
        sqlite_db.apply_migrations(
            "contracts",
            [
                Migration(version=1, name="one", sql="SELECT 1;"),
                Migration(version=3, name="three", sql="SELECT 3;"),
            ],
        )

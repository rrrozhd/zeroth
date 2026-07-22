"""Migration 013 adds reversible token-engine snapshot persistence."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from tests.conftest import requires_docker
from tests.storage.test_migration_coordination import _config


def test_token_snapshot_migration_round_trips_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'token-snapshot.db'}"
    config = _config(database_url)
    command.upgrade(config, "013")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert "token_engine_snapshots" in inspector.get_table_names()
        assert inspector.get_pk_constraint("token_engine_snapshots")["constrained_columns"] == [
            "run_id"
        ]
        foreign_keys = inspector.get_foreign_keys("token_engine_snapshots")
        assert any(
            key["referred_table"] == "runs" and key["constrained_columns"] == ["run_id"]
            for key in foreign_keys
        )
        with engine.connect() as connection:
            rows = connection.execute(sa.text("PRAGMA foreign_key_list(token_engine_snapshots)"))
            assert any(
                row._mapping["table"] == "runs" and row._mapping["on_delete"] == "CASCADE"
                for row in rows
            )
    finally:
        engine.dispose()

    command.downgrade(config, "012")
    engine = sa.create_engine(database_url)
    try:
        assert "token_engine_snapshots" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


@requires_docker
def test_token_snapshot_migration_runs_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _config(database_url)
    command.upgrade(config, "013")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert "token_engine_snapshots" in inspector.get_table_names()
        foreign_keys = inspector.get_foreign_keys("token_engine_snapshots")
        assert any(
            key["referred_table"] == "runs"
            and key["constrained_columns"] == ["run_id"]
            and key["options"].get("ondelete") == "CASCADE"
            for key in foreign_keys
        )
    finally:
        engine.dispose()

    command.downgrade(config, "012")
    engine = sa.create_engine(database_url)
    try:
        assert "token_engine_snapshots" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()

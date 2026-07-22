"""Migration 014 durably fences token snapshot writes after erasure."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from tests.conftest import requires_docker
from tests.storage.test_migration_coordination import _config


def _assert_fence_column(database_url: str) -> None:
    engine = sa.create_engine(database_url)
    try:
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns("runs")}
        fence = columns["token_snapshot_write_disabled"]
        assert not fence["nullable"]
        assert str(fence["default"]).strip("'()") == "0"
    finally:
        engine.dispose()


def test_token_snapshot_fence_migration_round_trips_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'snapshot-fence.db'}"
    config = _config(database_url)
    command.upgrade(config, "014")
    _assert_fence_column(database_url)

    command.downgrade(config, "013")
    engine = sa.create_engine(database_url)
    try:
        assert "token_snapshot_write_disabled" not in {
            column["name"] for column in sa.inspect(engine).get_columns("runs")
        }
    finally:
        engine.dispose()


@requires_docker
def test_token_snapshot_fence_migration_runs_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _config(database_url)
    command.upgrade(config, "014")
    _assert_fence_column(database_url)
    command.downgrade(config, "013")

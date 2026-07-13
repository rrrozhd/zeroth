from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from tests.conftest import requires_docker
from tests.storage.test_migration_coordination import _config


def test_retention_log_run_index_roundtrips(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'retention-log-index.db'}"
    config = _config(database_url)
    command.upgrade(config, "011")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes("retention_audit_log")
        }
        assert ("run_id", "created_at") in indexes
    finally:
        engine.dispose()

    command.downgrade(config, "010")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes("retention_audit_log")
        }
        assert ("run_id", "created_at") not in indexes
    finally:
        engine.dispose()


@requires_docker
def test_retention_log_run_index_exists_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    command.upgrade(_config(database_url), "011")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes("retention_audit_log")
        }
        assert ("run_id", "created_at") in indexes
    finally:
        engine.dispose()

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.conftest import requires_docker
from tests.storage.test_migration_coordination import _config


def _assert_cleanup_state_schema(database_url: str) -> None:
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert {
            "retention_cleanup_state",
            "retention_cleanup_operations",
        } <= set(inspector.get_table_names())
        state_pk = inspector.get_pk_constraint("retention_cleanup_state")
        assert state_pk["constrained_columns"] == ["authorization_log_id"]
        operation_pk = inspector.get_pk_constraint("retention_cleanup_operations")
        assert operation_pk["constrained_columns"] == [
            "authorization_log_id",
            "operation_id",
        ]
        state_indexes = {
            tuple(index["column_names"])
            for index in inspector.get_indexes("retention_cleanup_state")
        }
        assert ("tenant_id", "run_id") in state_indexes
        foreign_keys = inspector.get_foreign_keys("retention_cleanup_operations")
        assert any(
            key["referred_table"] == "retention_cleanup_state"
            and key["constrained_columns"] == ["authorization_log_id"]
            for key in foreign_keys
        )
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                rows = connection.execute(
                    sa.text("PRAGMA foreign_key_list(retention_cleanup_operations)")
                ).mappings()
                assert any(
                    row["table"] == "retention_cleanup_state" and row["on_delete"] == "CASCADE"
                    for row in rows
                )
        else:
            assert any(key["options"].get("ondelete") == "CASCADE" for key in foreign_keys)
    finally:
        engine.dispose()


def test_cleanup_state_migration_roundtrips_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'cleanup-state.db'}"
    config = _config(database_url)
    command.upgrade(config, "012")
    _assert_cleanup_state_schema(database_url)

    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO retention_audit_log
                        (log_id, tenant_id, run_id, action, reason, created_at)
                    VALUES ('auth-1', 'tenant-a', 'run-a', 'erasure_authorized',
                            'rte', '2026-07-12T00:00:00+00:00')
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO retention_cleanup_state
                        (authorization_log_id, tenant_id, run_id, reason,
                         generation, revision, created_at, updated_at)
                    VALUES ('auth-1', 'tenant-a', 'run-a', 'rte', 0, 0,
                            '2026-07-12T00:00:00+00:00',
                            '2026-07-12T00:00:00+00:00')
                    """
                )
            )
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO retention_cleanup_operations
                        (authorization_log_id, operation_id, status, revision, updated_at)
                    VALUES ('auth-1', 'op-1', 'unknown', 0,
                            '2026-07-12T00:00:00+00:00')
                    """
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, "011")
    engine = sa.create_engine(database_url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert "retention_cleanup_state" not in tables
        assert "retention_cleanup_operations" not in tables
    finally:
        engine.dispose()


@requires_docker
def test_cleanup_state_migration_runs_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    command.upgrade(_config(database_url), "012")
    _assert_cleanup_state_schema(database_url)

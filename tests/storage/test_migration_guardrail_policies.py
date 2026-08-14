"""Migration 027 round-trips guardrail policy and coordination tables."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from tests.conftest import requires_docker
from tests.storage.test_migration_coordination import _config

_TABLES = {"guardrail_policy_revisions", "guardrail_admission_state"}


def _insert_guardrail_state(database_url: str) -> None:
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """INSERT INTO guardrail_policy_revisions
                    (tenant_id, revision_id, scope_type, deployment_ref,
                     policy_json, changed_by, created_at)
                    VALUES ('tenant-a', 'revision-a', 'tenant', '',
                            :policy_json, 'admin-a', '2026-08-14T00:00:00Z')"""
                ),
                {"policy_json": '{"max_concurrency":2}'},
            )
            connection.execute(
                sa.text(
                    """INSERT INTO guardrail_admission_state
                    (tenant_id, workspace_id, workspace_scope, deployment_ref, created_at)
                    VALUES ('tenant-a', 'workspace-a', 'value:workspace-a',
                            'deployment-a', '2026-08-14T00:00:00Z')"""
                )
            )
    finally:
        engine.dispose()


def _assert_round_trip(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "027")
    _insert_guardrail_state(database_url)
    engine = sa.create_engine(database_url)
    try:
        assert _TABLES.issubset(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.downgrade(config, "026")
    engine = sa.create_engine(database_url)
    try:
        assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "027")
    engine = sa.create_engine(database_url)
    try:
        assert _TABLES.issubset(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            for table in _TABLES:
                assert (
                    connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
                )
    finally:
        engine.dispose()


def test_guardrail_policy_migration_round_trips_sqlite(tmp_path: Path) -> None:
    _assert_round_trip(f"sqlite:///{tmp_path / 'guardrail-policy.db'}")


@requires_docker
def test_guardrail_policy_migration_round_trips_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    _assert_round_trip(database_url)

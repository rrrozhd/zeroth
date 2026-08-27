"""Migration 029 round-trips database-ordered guardrail policy history."""

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
                    VALUES
                    ('tenant-a', 'revision-b', 'deployment', 'deployment-a',
                     :set_policy, 'admin-a', '2026-08-14T00:00:00Z'),
                    ('tenant-a', 'revision-a', 'deployment', 'deployment-a',
                     :reset_policy, 'admin-b', '2026-08-14T00:00:00Z'),
                    ('tenant-b', 'revision-a', 'tenant', '',
                     :set_policy, 'admin-c', '2026-08-13T00:00:00Z')"""
                ),
                {
                    "set_policy": '{"max_concurrency":2}',
                    "reset_policy": '{"reset_fields":["max_concurrency"]}',
                },
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
    command.upgrade(config, "028")
    _insert_guardrail_state(database_url)
    command.upgrade(config, "029")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert _TABLES.issubset(inspector.get_table_names())
        assert "revision_order" in {
            column["name"] for column in inspector.get_columns("guardrail_policy_revisions")
        }
        lookup = next(
            index
            for index in inspector.get_indexes("guardrail_policy_revisions")
            if index["name"] == "idx_guardrail_policy_lookup"
        )
        assert lookup["column_names"] == [
            "tenant_id",
            "scope_type",
            "deployment_ref",
            "revision_order",
        ]
        with engine.begin() as connection:
            rows = connection.execute(
                sa.text(
                    """SELECT tenant_id, revision_id, revision_order
                    FROM guardrail_policy_revisions ORDER BY revision_order"""
                )
            ).all()
            assert rows == [
                ("tenant-a", "revision-a", 1),
                ("tenant-a", "revision-b", 2),
                ("tenant-b", "revision-a", 3),
            ]
            connection.execute(
                sa.text(
                    """INSERT INTO guardrail_policy_revisions
                    (tenant_id, revision_id, scope_type, deployment_ref,
                     policy_json, changed_by, created_at)
                    VALUES ('tenant-a', 'revision-c', 'tenant', '',
                            :policy_json, 'admin-d', '2026-08-15T00:00:00Z')"""
                ),
                {"policy_json": '{"max_concurrency":4}'},
            )
            generated = connection.execute(
                sa.text(
                    """SELECT revision_order FROM guardrail_policy_revisions
                    WHERE tenant_id = 'tenant-a' AND revision_id = 'revision-c'"""
                )
            ).scalar_one()
            assert generated > rows[-1].revision_order
    finally:
        engine.dispose()

    command.downgrade(config, "028")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert _TABLES.issubset(inspector.get_table_names())
        assert "revision_order" not in {
            column["name"] for column in inspector.get_columns("guardrail_policy_revisions")
        }
        assert inspector.get_pk_constraint("guardrail_policy_revisions")["constrained_columns"] == [
            "tenant_id",
            "revision_id",
        ]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM guardrail_policy_revisions")
                ).scalar_one()
                == 4
            )
    finally:
        engine.dispose()

    command.upgrade(config, "029")
    engine = sa.create_engine(database_url)
    try:
        assert _TABLES.issubset(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(DISTINCT revision_order) FROM guardrail_policy_revisions")
                ).scalar_one()
                == 4
            )
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM guardrail_admission_state")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_guardrail_policy_migration_round_trips_sqlite(tmp_path: Path) -> None:
    _assert_round_trip(f"sqlite:///{tmp_path / 'guardrail-policy.db'}")


@requires_docker
def test_guardrail_policy_migration_round_trips_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    _assert_round_trip(database_url)

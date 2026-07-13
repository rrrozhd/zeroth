"""Migration 010 adds portable audit and retention coordination state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.conftest import requires_docker


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/core/migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _schema(database_url: str) -> tuple[set[str], set[str], set[tuple[str, ...]]]:
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("node_audits")}
        indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes("node_audits")
        }
        return tables, columns, indexes
    finally:
        engine.dispose()


def _insert_legacy_audits(database_url: str) -> None:
    engine = sa.create_engine(database_url)
    rows = [
        ("a-later", "run-a", "2026-01-01T00:00:02+00:00", "digest-2"),
        ("a-first-z", "run-a", "2026-01-01T00:00:01+00:00", "digest-z"),
        ("a-first-a", "run-a", "2026-01-01T00:00:01+00:00", "digest-a"),
        ("b-only", "run-b", "2026-01-01T00:00:03+00:00", "digest-b"),
    ]
    statement = sa.text(
        """
        INSERT INTO node_audits (
            audit_id, run_id, thread_id, node_id, graph_version_ref,
            deployment_ref, tenant_id, workspace_id, created_at, record_json
        ) VALUES (
            :audit_id, :run_id, NULL, 'node', 'graph:v1',
            'deployment', 'default', NULL, :created_at, :record_json
        )
        """
    )
    with engine.begin() as connection:
        for audit_id, run_id, created_at, digest in rows:
            connection.execute(
                statement,
                {
                    "audit_id": audit_id,
                    "run_id": run_id,
                    "created_at": created_at,
                    "record_json": json.dumps({"record_digest": digest}),
                },
            )
    engine.dispose()


def test_coordination_migration_roundtrips_and_backfills_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'coordination-migration.db'}"
    config = _config(database_url)
    command.upgrade(config, "009")
    _insert_legacy_audits(database_url)

    backfill_statements: list[str] = []

    def capture_backfill(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split())
        if normalized.startswith("WITH ranked AS") and "chain_sequence" in normalized:
            backfill_statements.append(normalized)

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", capture_backfill)
    try:
        command.upgrade(config, "010")
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", capture_backfill)
    assert len(backfill_statements) == 1
    assert (
        "UPDATE node_audits AS target SET chain_sequence = ranked.chain_sequence FROM ranked"
        in backfill_statements[0]
    )
    assert "SET chain_sequence = ( SELECT" not in backfill_statements[0]
    tables, columns, indexes = _schema(database_url)
    assert {"audit_chain_heads", "retention_coordination"} <= tables
    assert "chain_sequence" in columns
    assert ("run_id", "chain_sequence") in indexes

    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        inspector = sa.inspect(connection)
        assert {column["name"] for column in inspector.get_columns("audit_chain_heads")} == {
            "run_id",
            "head_digest",
            "next_sequence",
            "updated_at",
        }
        assert {
            column["name"] for column in inspector.get_columns("retention_coordination")
        } == {"tenant_id", "updated_at"}
        rows = connection.execute(
            sa.text(
                "SELECT audit_id, chain_sequence FROM node_audits "
                "ORDER BY run_id, chain_sequence"
            )
        ).mappings().all()
        assert [(row["audit_id"], row["chain_sequence"]) for row in rows] == [
            ("a-first-a", 1),
            ("a-first-z", 2),
            ("a-later", 3),
            ("b-only", 1),
        ]
        # Nullable preserves compatibility for writers deployed before migration 010.
        connection.execute(
            sa.text(
                """
                INSERT INTO node_audits (
                    audit_id, run_id, node_id, graph_version_ref, deployment_ref,
                    created_at, record_json
                ) VALUES ('legacy-null', 'run-c', 'node', 'graph:v1', 'deployment',
                          '2026-01-01T00:00:04+00:00', '{}')
                """
            )
        )
        assert connection.execute(
            sa.text("SELECT chain_sequence FROM node_audits WHERE audit_id='legacy-null'")
        ).scalar_one() is None
    engine.dispose()

    command.downgrade(config, "009")
    tables, columns, indexes = _schema(database_url)
    assert "audit_chain_heads" not in tables
    assert "retention_coordination" not in tables
    assert "chain_sequence" not in columns
    assert ("run_id", "chain_sequence") not in indexes

    command.upgrade(config, "010")
    tables, columns, indexes = _schema(database_url)
    assert {"audit_chain_heads", "retention_coordination"} <= tables
    assert "chain_sequence" in columns
    assert ("run_id", "chain_sequence") in indexes


def test_coordination_migration_enforces_per_run_sequence_uniqueness(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'coordination-unique.db'}"
    command.upgrade(_config(database_url), "010")
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            values = {
                "run_id": "same-run",
                "node_id": "node",
                "graph_version_ref": "graph:v1",
                "deployment_ref": "deployment",
                "created_at": "2026-01-01T00:00:00+00:00",
                "record_json": "{}",
                "chain_sequence": 1,
            }
            sql = sa.text(
                """
                INSERT INTO node_audits (
                    audit_id, run_id, node_id, graph_version_ref, deployment_ref,
                    created_at, record_json, chain_sequence
                ) VALUES (:audit_id, :run_id, :node_id, :graph_version_ref,
                          :deployment_ref, :created_at, :record_json, :chain_sequence)
                """
            )
            connection.execute(sql, values | {"audit_id": "first"})
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sql, values | {"audit_id": "second"})
    finally:
        engine.dispose()


@requires_docker
def test_coordination_backfill_uses_window_ordering_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _config(database_url)
    command.upgrade(config, "010")
    command.downgrade(config, "009")
    try:
        _insert_legacy_audits(database_url)
        backfill_statements: list[str] = []

        def capture_backfill(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.split())
            if normalized.startswith("WITH ranked AS") and "chain_sequence" in normalized:
                backfill_statements.append(normalized)

        sa.event.listen(sa.engine.Engine, "before_cursor_execute", capture_backfill)
        try:
            command.upgrade(config, "010")
        finally:
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", capture_backfill)
        assert len(backfill_statements) == 1

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    sa.text(
                        "SELECT audit_id, chain_sequence FROM node_audits "
                        "WHERE audit_id IN ('a-later', 'a-first-z', 'a-first-a', 'b-only') "
                        "ORDER BY run_id, chain_sequence"
                    )
                ).mappings().all()
                plan = connection.execute(
                    sa.text(f"EXPLAIN (FORMAT JSON) {backfill_statements[0]}")
                ).scalar_one()
            assert [(row["audit_id"], row["chain_sequence"]) for row in rows] == [
                ("a-first-a", 1),
                ("a-first-z", 2),
                ("a-later", 3),
                ("b-only", 1),
            ]
            serialized_plan = json.dumps(plan)
            assert "WindowAgg" in serialized_plan
            assert "SubPlan" not in serialized_plan
        finally:
            engine.dispose()
    finally:
        command.upgrade(config, "010")
        engine = sa.create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "DELETE FROM node_audits "
                        "WHERE audit_id IN ('a-later', 'a-first-z', 'a-first-a', 'b-only')"
                    )
                )
        finally:
            engine.dispose()

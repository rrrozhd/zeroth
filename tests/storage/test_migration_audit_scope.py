"""Migration 024 partitions audit coordination by durable tenant ownership."""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from tests.conftest import requires_docker


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _legacy_audit(connection, *, audit_id: str, run_id: str, tenant_id: str, sequence: int) -> None:
    payload = {
        "audit_id": audit_id,
        "run_id": run_id,
        "node_id": "node",
        "graph_version_ref": "graph:v1",
        "deployment_ref": "deployment:v1",
        "tenant_id": tenant_id,
        "status": "completed",
        "chain_sequence": sequence,
    }
    connection.execute(
        text(
            """INSERT INTO node_audits (
                audit_id, run_id, node_id, graph_version_ref, deployment_ref,
                tenant_id, created_at, chain_sequence, record_json
            ) VALUES (:audit_id, :run_id, 'node', 'graph:v1', 'deployment:v1',
                      :tenant_id, '2026-08-11', :sequence, :record_json)"""
        ),
        {
            "audit_id": audit_id,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "sequence": sequence,
            "record_json": json.dumps(payload),
        },
    )


def test_upgrade_preserves_rows_and_installs_tenant_chain_identity(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'audit-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "023")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _legacy_audit(
            connection,
            audit_id="audit-1",
            run_id="run-1",
            tenant_id="tenant-a",
            sequence=1,
        )
        connection.execute(
            text(
                """INSERT INTO audit_chain_heads (
                    run_id, head_digest, next_sequence, updated_at
                ) VALUES ('run-1', 'digest-1', 2, '2026-08-11')"""
            )
        )
    engine.dispose()

    command.upgrade(config, "024")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
            "tenant_id",
            "run_id",
        ]
        head_columns = {
            column["name"]: column for column in inspector.get_columns("audit_chain_heads")
        }
        audit_columns = {column["name"]: column for column in inspector.get_columns("node_audits")}
        assert head_columns["tenant_id"]["nullable"] is False
        assert head_columns["tenant_id"]["default"] is None
        assert audit_columns["tenant_id"]["nullable"] is False
        assert audit_columns["tenant_id"]["default"] is None
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT tenant_id, run_id, head_digest, next_sequence FROM audit_chain_heads")
            ).one() == ("tenant-a", "run-1", "digest-1", 2)
            assert connection.execute(text("SELECT COUNT(*) FROM node_audits")).scalar_one() == 1
    finally:
        engine.dispose()


def test_upgrade_refuses_historical_run_shared_across_tenants_before_ddl(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'mixed-audit-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "023")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _legacy_audit(
            connection,
            audit_id="audit-a",
            run_id="shared-run",
            tenant_id="tenant-a",
            sequence=1,
        )
        _legacy_audit(
            connection,
            audit_id="audit-b",
            run_id="shared-run",
            tenant_id="tenant-b",
            sequence=2,
        )
        connection.execute(
            text(
                """INSERT INTO audit_chain_heads (
                    run_id, head_digest, next_sequence, updated_at
                ) VALUES ('shared-run', 'digest', 3, '2026-08-11')"""
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="mixed-tenant run_id 'shared-run'"):
        command.upgrade(config, "024")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
        "run_id"
    ]
    assert not inspect(engine).has_table("audit_chain_heads_legacy_unscoped")
    engine.dispose()


def test_fresh_head_has_scoped_audit_chain_identity(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fresh-audit-scope.db'}"
    command.upgrade(_config(database_url), "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
            "tenant_id",
            "run_id",
        ]
        assert {column["name"]: column for column in inspector.get_columns("node_audits")}[
            "tenant_id"
        ]["nullable"] is False
    finally:
        engine.dispose()


def test_downgrade_collision_fails_before_ddl_and_is_retryable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'audit-scope-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "024")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for tenant in ("tenant-a", "tenant-b"):
            connection.execute(
                text(
                    """INSERT INTO audit_chain_heads (
                        tenant_id, run_id, head_digest, next_sequence, updated_at
                    ) VALUES (:tenant, 'shared-run', NULL, 1, '2026-08-11')"""
                ),
                {"tenant": tenant},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate global run_id"):
        command.downgrade(config, "023")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
        "tenant_id",
        "run_id",
    ]
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_chain_heads WHERE tenant_id = 'tenant-b'"))
    engine.dispose()

    command.downgrade(config, "023")
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
            "run_id"
        ]
    finally:
        engine.dispose()


@requires_docker
def test_upgrade_preserves_scoped_audit_rows_on_postgres(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "023")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _legacy_audit(
                connection,
                audit_id="audit-pg",
                run_id="run-pg",
                tenant_id="tenant-pg",
                sequence=1,
            )
            connection.execute(
                text(
                    """INSERT INTO audit_chain_heads (
                        run_id, head_digest, next_sequence, updated_at
                    ) VALUES ('run-pg', 'digest-pg', 2, '2026-08-11')"""
                )
            )
        engine.dispose()

        command.upgrade(config, "024")
        engine = create_engine(database_url)
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("audit_chain_heads")["constrained_columns"] == [
            "tenant_id",
            "run_id",
        ]
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT tenant_id, run_id, head_digest, next_sequence "
                    "FROM audit_chain_heads WHERE run_id = 'run-pg'"
                )
            ).one() == ("tenant-pg", "run-pg", "digest-pg", 2)
    finally:
        engine.dispose()
        command.upgrade(config, "head")

"""Migration 022 gives checkpoint rows durable tenant/workspace ownership."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _thread(connection, tenant: str, thread_id: str = "checkpoint-thread") -> None:
    connection.execute(
        text(
            """INSERT INTO threads (
                thread_id, graph_version_ref, deployment_ref, status,
                participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                memory_bindings, run_ids, created_at, updated_at, tenant_id,
                workspace_id, workspace_scope
            ) VALUES (:thread_id, 'graph', 'deployment', 'active', '[]', '[]', '[]',
                      '[]', '[]', '2026-08-09', '2026-08-09', :tenant, NULL, 'null')"""
        ),
        {"thread_id": thread_id, "tenant": tenant},
    )


def _checkpoint(connection, checkpoint_id: str = "checkpoint-1") -> None:
    connection.execute(
        text(
            """INSERT INTO run_checkpoints (
                checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at
            ) VALUES (:checkpoint_id, 'run-1', 'checkpoint-thread', 0, '{}', '2026-08-09')"""
        ),
        {"checkpoint_id": checkpoint_id},
    )


def test_upgrade_backfills_checkpoint_owner_and_composite_identity(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'checkpoint-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "021")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _thread(connection, "tenant-a")
        _checkpoint(connection)
    engine.dispose()

    command.upgrade(config, "022")
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_pk_constraint("run_checkpoints")["constrained_columns"] == [
            "tenant_id",
            "workspace_scope",
            "checkpoint_id",
        ]
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT tenant_id, workspace_id, workspace_scope "
                    "FROM run_checkpoints WHERE checkpoint_id='checkpoint-1'"
                )
            ).one()
        assert row == ("tenant-a", None, "null")
    finally:
        engine.dispose()


def test_upgrade_ambiguity_fails_before_ddl_and_is_retryable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'checkpoint-ambiguous.db'}"
    config = _config(database_url)
    command.upgrade(config, "021")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _thread(connection, "tenant-a")
        _thread(connection, "tenant-b")
        _checkpoint(connection)
    engine.dispose()

    with pytest.raises(RuntimeError, match="ambiguous or missing thread owner"):
        command.upgrade(config, "022")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("run_checkpoints")["constrained_columns"] == [
        "checkpoint_id"
    ]
    assert not inspect(engine).has_table("run_checkpoints_legacy_unscoped")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM run_checkpoints")).scalar_one() == 1
        connection.execute(text("DELETE FROM threads WHERE tenant_id='tenant-b'"))
    engine.dispose()
    command.upgrade(config, "022")


def test_downgrade_collision_fails_before_ddl_and_is_retryable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'checkpoint-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "022")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for tenant in ("tenant-a", "tenant-b"):
            connection.execute(
                text(
                    """INSERT INTO run_checkpoints (
                        checkpoint_id, run_id, thread_id, checkpoint_order, state_json,
                        created_at, tenant_id, workspace_id, workspace_scope
                    ) VALUES ('shared-checkpoint', :run_id, 'thread', 0, '{}', '2026-08-09',
                              :tenant, NULL, 'null')"""
                ),
                {"run_id": f"run-{tenant}", "tenant": tenant},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate global checkpoint_id"):
        command.downgrade(config, "021")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("run_checkpoints")["constrained_columns"] == [
        "tenant_id",
        "workspace_scope",
        "checkpoint_id",
    ]
    assert not inspect(engine).has_table("run_checkpoints_scoped_owner")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM run_checkpoints")).scalar_one() == 2
        connection.execute(text("DELETE FROM run_checkpoints WHERE tenant_id='tenant-b'"))
    engine.dispose()
    command.downgrade(config, "021")

"""Migration 021 makes logical thread IDs unique within tenant/workspace scope."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
import pytest


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_thread_scope_migration_preserves_legacy_rows_and_allows_scoped_duplicates(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'thread-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "020")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO threads (
                    thread_id, graph_version_ref, deployment_ref, status,
                    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                    memory_bindings, run_ids, created_at, updated_at, tenant_id, workspace_id
                ) VALUES (
                    'shared-id', 'graph:v1', 'deployment', 'active',
                    '[]', '[]', '[]', '[]', '[]', '2026-08-09', '2026-08-09',
                    'tenant-a', 'workspace-a'
                )
                """
            )
        )
    engine.dispose()

    command.upgrade(config, "021")
    engine = create_engine(database_url)
    try:
        primary_key = inspect(engine).get_pk_constraint("threads")["constrained_columns"]
        assert primary_key == ["tenant_id", "workspace_scope", "thread_id"]
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO threads (
                        thread_id, graph_version_ref, deployment_ref, status,
                        participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                        memory_bindings, run_ids, created_at, updated_at, tenant_id,
                        workspace_id, workspace_scope
                    ) VALUES (
                        'shared-id', 'graph:v1', 'deployment', 'active',
                        '[]', '[]', '[]', '[]', '[]', '2026-08-09', '2026-08-09',
                        'tenant-b', 'workspace-b', 'value:workspace-b'
                    )
                    """
                )
            )
            rows = connection.execute(
                text("SELECT tenant_id, workspace_scope FROM threads ORDER BY tenant_id")
            ).all()
        assert rows == [
            ("tenant-a", "value:workspace-a"),
            ("tenant-b", "value:workspace-b"),
        ]
    finally:
        engine.dispose()


def test_upgrade_canonicalizes_legacy_null_tenant_before_rebuild(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'null-tenant.db'}"
    config = _config(database_url)
    command.upgrade(config, "020")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                """INSERT INTO threads (
                    thread_id, graph_version_ref, deployment_ref, status,
                    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                    memory_bindings, run_ids, created_at, updated_at, tenant_id
                ) VALUES ('legacy-null', 'graph', 'deployment', 'active', '[]', '[]',
                          '[]', '[]', '[]', '2026-08-09', '2026-08-09', NULL)"""
            )
        )
    engine.dispose()

    command.upgrade(config, "021")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT tenant_id, workspace_scope FROM threads WHERE thread_id='legacy-null'")
        ).one()
    engine.dispose()
    assert row == ("default", "null")


def test_upgrade_preflight_failure_leaves_legacy_schema_retryable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'invalid-tenant.db'}"
    config = _config(database_url)
    command.upgrade(config, "020")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO threads (
                    thread_id, graph_version_ref, deployment_ref, status,
                    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                    memory_bindings, run_ids, created_at, updated_at, tenant_id
                ) VALUES ('invalid', 'graph', 'deployment', 'active', '[]', '[]',
                          '[]', '[]', '[]', '2026-08-09', '2026-08-09', '   ')"""
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="blank tenant_id"):
        command.upgrade(config, "021")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("threads")["constrained_columns"] == ["thread_id"]
    assert not inspect(engine).has_table("threads_legacy_global_id")
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT tenant_id FROM threads WHERE thread_id='invalid'")
            ).scalar_one()
            == "   "
        )
        connection.execute(text("UPDATE threads SET tenant_id='default' WHERE thread_id='invalid'"))
    engine.dispose()
    command.upgrade(config, "021")


def test_downgrade_duplicate_preflight_leaves_scoped_schema_and_data_retryable(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'downgrade-duplicate.db'}"
    config = _config(database_url)
    command.upgrade(config, "021")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for tenant in ("tenant-a", "tenant-b"):
            connection.execute(
                text(
                    """INSERT INTO threads (
                        thread_id, graph_version_ref, deployment_ref, status,
                        participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                        memory_bindings, run_ids, created_at, updated_at, tenant_id,
                        workspace_scope
                    ) VALUES ('duplicate', 'graph', 'deployment', 'active', '[]', '[]',
                              '[]', '[]', '[]', '2026-08-09', '2026-08-09', :tenant,
                              'null')"""
                ),
                {"tenant": tenant},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate global thread_id"):
        command.downgrade(config, "020")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("threads")["constrained_columns"] == [
        "tenant_id",
        "workspace_scope",
        "thread_id",
    ]
    assert not inspect(engine).has_table("threads_scoped_identity")
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT tenant_id FROM threads WHERE thread_id='duplicate' ORDER BY tenant_id")
        ).scalars().all() == ["tenant-a", "tenant-b"]
        connection.execute(text("DELETE FROM threads WHERE tenant_id='tenant-b'"))
    engine.dispose()
    command.downgrade(config, "020")

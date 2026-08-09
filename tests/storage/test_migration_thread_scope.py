"""Migration 021 makes logical thread IDs unique within tenant/workspace scope."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


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

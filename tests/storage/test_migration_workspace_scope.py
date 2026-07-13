"""Migration 009 adds reversible workspace ownership to graph versions."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from tests.conftest import requires_docker


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/core/migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _graph_columns_and_indexes(database_url: str) -> tuple[set[str], set[tuple[str, ...]]]:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("graph_versions")}
        indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes("graph_versions")
        }
        return columns, indexes
    finally:
        engine.dispose()


def test_workspace_scope_migration_is_reversible_on_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'workspace-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "008")

    command.upgrade(config, "009")
    columns, indexes = _graph_columns_and_indexes(database_url)
    assert "workspace_id" in columns
    assert ("tenant_id", "workspace_id") in indexes

    command.downgrade(config, "008")
    columns, indexes = _graph_columns_and_indexes(database_url)
    assert "workspace_id" not in columns
    assert ("tenant_id", "workspace_id") not in indexes

    command.upgrade(config, "009")
    columns, indexes = _graph_columns_and_indexes(database_url)
    assert "workspace_id" in columns
    assert ("tenant_id", "workspace_id") in indexes


@requires_docker
async def test_workspace_scope_migration_roundtrips_on_postgres(
    postgres_database, postgres_container
) -> None:
    url = postgres_container.get_connection_url()
    config = _config(url.replace("psycopg2", "psycopg"))

    command.downgrade(config, "008")
    try:
        async with postgres_database.transaction() as connection:
            removed_column = await connection.fetch_one(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_name = 'graph_versions' AND column_name = 'workspace_id'
                """
            )
        assert removed_column is None
    finally:
        command.upgrade(config, "009")

    async with postgres_database.transaction() as connection:
        column = await connection.fetch_one(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_name = 'graph_versions' AND column_name = 'workspace_id'
            """
        )
        index = await connection.fetch_one(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE tablename = 'graph_versions'
              AND indexname = 'idx_graph_versions_tenant_workspace'
            """
        )

    assert column == {"is_nullable": "YES"}
    assert index is not None
    assert "(tenant_id, workspace_id)" in index["indexdef"]

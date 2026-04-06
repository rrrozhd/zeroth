"""Postgres backend integration tests using testcontainers.

These tests verify that key repositories produce identical behavior on both
SQLite and Postgres backends, and that the database factory correctly selects
the right backend based on settings.
"""

from __future__ import annotations

from tests.conftest import requires_docker


@requires_docker
class TestGraphRepositoryDualBackend:
    """Graph repository tests that run on both SQLite and Postgres."""

    async def test_save_and_get_graph(self, dual_database):
        from tests.graph.test_models import build_graph
        from zeroth.graph.repository import GraphRepository

        repo = GraphRepository(dual_database)
        graph = build_graph()

        saved = await repo.save(graph)
        loaded = await repo.get(graph.graph_id)

        assert saved == graph
        assert loaded == graph

    async def test_publish_graph(self, dual_database):
        from tests.graph.test_models import build_graph
        from zeroth.graph.models import GraphStatus
        from zeroth.graph.repository import GraphRepository

        repo = GraphRepository(dual_database)
        graph = await repo.create(build_graph())

        published = await repo.publish(graph.graph_id, graph.version)

        assert published.status == GraphStatus.PUBLISHED
        assert (await repo.get(graph.graph_id)).status == GraphStatus.PUBLISHED


@requires_docker
class TestRunRepositoryDualBackend:
    async def test_put_and_get_run(self, dual_database):
        from zeroth.runs.models import Run
        from zeroth.runs.repository import RunRepository

        repo = RunRepository(dual_database)
        run = await repo.create(
            Run(
                graph_version_ref="graph:v1",
                deployment_ref="deployment:v1",
            )
        )

        loaded = await repo.get(run.run_id)

        assert loaded is not None
        assert loaded.run_id == run.run_id
        assert loaded.deployment_ref == "deployment:v1"

    async def test_count_pending(self, dual_database):
        from zeroth.runs.models import Run
        from zeroth.runs.repository import RunRepository

        repo = RunRepository(dual_database)

        await repo.create(
            Run(
                graph_version_ref="graph:v1",
                deployment_ref="deployment:count",
            )
        )
        await repo.create(
            Run(
                graph_version_ref="graph:v1",
                deployment_ref="deployment:count",
            )
        )

        count = await repo.count_pending("deployment:count")

        assert count == 2


@requires_docker
class TestDatabaseFactory:
    async def test_factory_creates_sqlite(self, tmp_path):
        from zeroth.config.settings import ZerothSettings
        from zeroth.storage.async_sqlite import AsyncSQLiteDatabase
        from zeroth.storage.factory import create_database

        settings = ZerothSettings(
            database={"backend": "sqlite", "sqlite_path": str(tmp_path / "test.db")}
        )
        db = await create_database(settings)
        assert isinstance(db, AsyncSQLiteDatabase)
        await db.close()

    async def test_factory_creates_postgres(self, postgres_container):
        from zeroth.config.settings import ZerothSettings
        from zeroth.storage.async_postgres import AsyncPostgresDatabase
        from zeroth.storage.factory import create_database

        url = postgres_container.get_connection_url()
        dsn = url.replace("postgresql+psycopg2://", "postgresql://")
        settings = ZerothSettings(database={"backend": "postgres", "postgres_dsn": dsn})
        db = await create_database(settings)
        assert isinstance(db, AsyncPostgresDatabase)
        await db.close()


@requires_docker
class TestAlembicMigrations:
    def test_migrations_run_on_postgres(self, postgres_container):
        from zeroth.service.bootstrap import run_migrations

        url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
        run_migrations(url)  # Should not raise

    def test_migrations_run_on_sqlite(self, tmp_path):
        from zeroth.service.bootstrap import run_migrations

        run_migrations(f"sqlite:///{tmp_path}/mig_test.db")  # Should not raise

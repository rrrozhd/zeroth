from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


@pytest.fixture
async def async_database(tmp_path: Path) -> AsyncSQLiteDatabase:
    """Async SQLite database for tests. Runs Alembic migrations on a temp DB."""
    db_path = str(tmp_path / "zeroth.db")
    run_migrations(f"sqlite:///{db_path}")
    db = AsyncSQLiteDatabase(path=db_path)
    yield db
    await db.close()


# Alias so every test that used the old `sqlite_db` fixture works with the
# async database after the Plan-02 repository rewrite.
@pytest.fixture
async def sqlite_db(async_database: AsyncSQLiteDatabase) -> AsyncSQLiteDatabase:
    return async_database


def _docker_available() -> bool:
    """Check whether Docker is available on this system."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(not _docker_available(), reason="Docker not available")


@pytest.fixture(scope="session")
def postgres_container():
    """Session-scoped Postgres container for integration tests."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17") as pg:
        yield pg


@pytest.fixture
async def postgres_database(postgres_container):
    """Async Postgres database for tests."""
    from zeroth.core.storage.async_postgres import AsyncPostgresDatabase

    url = postgres_container.get_connection_url()
    sa_url = url.replace("psycopg2", "psycopg")
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")

    run_migrations(sa_url)
    db = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=3)
    yield db
    await db.close()

    # Clean tables between tests
    import psycopg

    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn, conn.transaction():
        for table in [
            "node_audits",
            "approvals",
            "runs",
            "threads",
            "run_checkpoints",
            "graph_versions",
            "contract_versions",
            "deployment_versions",
            "rate_limit_tokens",
            "daily_quotas",
        ]:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")


@pytest.fixture(params=["sqlite", "postgres"])
async def dual_database(request, tmp_path, postgres_container):
    """Database fixture parametrized for both backends."""
    if request.param == "sqlite":
        db_path = str(tmp_path / "test.db")
        run_migrations(f"sqlite:///{db_path}")
        db = AsyncSQLiteDatabase(path=db_path)
        yield db
        await db.close()
    else:
        from zeroth.core.storage.async_postgres import AsyncPostgresDatabase

        url = postgres_container.get_connection_url()
        sa_url = url.replace("psycopg2", "psycopg")
        dsn = url.replace("postgresql+psycopg2://", "postgresql://")

        run_migrations(sa_url)
        db = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=3)
        yield db
        await db.close()

        import psycopg

        conn = await psycopg.AsyncConnection.connect(dsn)
        async with conn, conn.transaction():
            for table in [
                "node_audits",
                "approvals",
                "runs",
                "threads",
                "run_checkpoints",
                "graph_versions",
                "contract_versions",
                "deployment_versions",
                "rate_limit_tokens",
                "daily_quotas",
            ]:
                await conn.execute(f"TRUNCATE TABLE {table} CASCADE")

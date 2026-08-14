from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import weakref
from pathlib import Path

import pytest

# The bundled Regulus control plane fails closed on the placeholder JWT secret
# ("change-me") when mounted in-process. Set a real test secret before any
# econ_plane import so the mount tests exercise the production-like path rather
# than the insecure-default guard. Must run at collection import time.
os.environ.setdefault("ECP_JWT_SECRET", "test-econ-jwt-secret-not-a-real-key")

# econ_plane's engine binds its database URL at import time; without an
# override it writes ./econ_plane.db in the repo, so rows ingested by one
# test session survive into the next and idempotent-insert tests flake.
os.environ.setdefault(
    "ECP_DATABASE_URL",
    f"sqlite+pysqlite:///{tempfile.mkdtemp(prefix='zeroth-econ-test-')}/econ_plane.db",
)

# The service bootstrap's default filesystem artifact store writes erasure
# receipts under ./.zeroth/artifacts; ZerothSettings is a lazy env-reading
# singleton, so point the base dir at a session temp dir before any test
# triggers get_settings().
os.environ.setdefault(
    "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR",
    tempfile.mkdtemp(prefix="zeroth-artifacts-test-"),
)

from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase  # noqa: E402
from zeroth.service.bootstrap.migrations import run_migrations  # noqa: E402


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


_CLEANUP_TABLES = (
    "node_audits",
    "approvals",
    "guardrail_admission_state",
    "guardrail_policy_revisions",
    "rate_limit_buckets",
    "quota_counters",
    "runs",
    "threads",
    "run_checkpoints",
    "graph_versions",
    "contract_versions",
    "deployment_versions",
    "side_effect_operations",
)


async def _truncate_present(conn, tables: tuple[str, ...]) -> None:
    """Truncate only the tables that exist on this connection.

    Migration tests deliberately leave the schema at an older revision, so a
    table introduced by a later revision may legitimately be absent. Postgres
    has no ``TRUNCATE ... IF EXISTS``, and one missing table would abort the
    whole teardown transaction -- so the set is intersected with reality first.
    """
    rows = await (
        await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    ).fetchall()
    present = {row[0] for row in rows}
    for table in tables:
        if table in present:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")


@pytest.fixture(scope="session")
def postgres_container():
    """Session-scoped Postgres container for integration tests."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17") as pg:
        yield pg


@pytest.fixture
async def postgres_database(postgres_container):
    """Async Postgres database for tests."""
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

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
        await _truncate_present(conn, _CLEANUP_TABLES)


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
        from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

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
            await _truncate_present(conn, _CLEANUP_TABLES)


@pytest.fixture(scope="session")
def _otel_exporter():
    """Session-wide in-memory OTel exporter.

    OpenTelemetry only honours the first global TracerProvider, so it must be set
    exactly once per session; individual tests clear the exporter between runs.
    """
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )  # synchronous: spans flush immediately
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def otel_spans(_otel_exporter):
    """Enable tracing for one test and yield the cleared in-memory span exporter."""
    from zeroth.platform.observability import tracing

    _otel_exporter.clear()
    tracing._TRACING_ENABLED = True
    try:
        yield _otel_exporter
    finally:
        tracing._TRACING_ENABLED = False
        _otel_exporter.clear()


@pytest.fixture(autouse=True)
def _tripwire_repo_root_residue(request):
    """Fail the leaking test when state escapes into the repo root.

    Repo-root databases and artifact directories survive across sessions and
    turn idempotency assertions into order-dependent flakes; catching the
    leak at the offending test keeps the diagnosis one stack trace away.
    """
    yield
    residue = [p for p in ("econ_plane.db", ".zeroth") if os.path.exists(p)]
    if residue:
        raise AssertionError(f"repo-root residue {residue} created during {request.node.nodeid}")


class ContentCaptureClassifier:
    """Classify every record into content -- the deployment posture that keeps it.

    ``AuditRepository.write`` applies a metadata-only capture policy to every
    record that has not been classified already, so a test asserting on stored
    prompts, tool outcomes, denial reasons or free-form runtime metadata is
    asserting about a deployment that deliberately retains content. Saying so
    explicitly is the point: the default posture keeps none of it.
    """

    def classify(self, record: object) -> str:
        """Answer ``content`` whatever the record holds."""
        del record
        from zeroth.governance.audit.capture_policy import CaptureDecision

        return CaptureDecision.CONTENT.value


_CONTENT_CAPTURED: weakref.WeakSet = weakref.WeakSet()


def content_capture(repository):
    """Opt one audit repository into retaining content, returning it for chaining.

    Idempotent per repository, because ``configure_capture`` is deliberately
    one-shot: a test that seeds several records must not have to remember which
    call was the first.
    """
    if repository not in _CONTENT_CAPTURED:
        repository.configure_capture(ContentCaptureClassifier())
        _CONTENT_CAPTURED.add(repository)
    return repository

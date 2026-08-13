"""Tests for PgvectorMemoryConnector.

Unit tests mock psycopg and litellm to test the connector logic
without requiring a real Postgres instance.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope
from zeroth.integrations.memory.pgvector_connector import PgvectorMemoryConnector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536
FAKE_CONNINFO = "postgresql://test:test@localhost:5432/testdb"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_row(key="doc1", value=None, scope="shared", target="__shared__"):
    """Build a fake DB row tuple matching the SELECT column order."""
    if value is None:
        value = {"text": "hello"}
    return (
        key,
        json.dumps(value),
        scope,
        target,
        {},
        NOW,
        NOW,
    )


@pytest.fixture
def _mock_litellm():
    """Patch litellm.aembedding to return a fake embedding."""
    resp = MagicMock()
    resp.data = [{"embedding": FAKE_EMBEDDING}]
    with patch("zeroth.integrations.memory.pgvector_connector.litellm") as mock_mod:
        mock_mod.aembedding = AsyncMock(return_value=resp)
        yield mock_mod


@pytest.fixture
def _mock_conn():
    """Build a mock async psycopg connection."""
    conn = AsyncMock()
    # Make the context manager work: async with conn: ...
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


@pytest.fixture
def connector(_mock_conn, _mock_litellm):
    """Create a PgvectorMemoryConnector with mocked connection factory."""
    with patch(
        "zeroth.integrations.memory.pgvector_connector.register_vector_async", new=AsyncMock()
    ):
        c = PgvectorMemoryConnector(
            conn_factory=AsyncMock(return_value=_mock_conn),
            table_name="test_vectors",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        )
        # Skip schema setup in tests
        c._setup_done = True
        yield c


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_isinstance_memory_connector(self):
        """PgvectorMemoryConnector satisfies GovernAI MemoryConnector protocol."""
        assert issubclass(PgvectorMemoryConnector, MemoryConnector)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_stores_entry(self, connector, _mock_conn, _mock_litellm):
        await connector.write(
            "doc1", {"text": "hello world"}, MemoryScope.SHARED, target="__shared__"
        )
        # Verify embedding was generated
        _mock_litellm.aembedding.assert_awaited_once()
        call_kwargs = _mock_litellm.aembedding.call_args
        assert call_kwargs.kwargs["model"] == "text-embedding-3-small"

        # Verify SQL was executed (INSERT ... ON CONFLICT)
        _mock_conn.execute.assert_awaited()
        sql_call = _mock_conn.execute.call_args_list[-1]
        sql = sql_call.args[0]
        assert "INSERT INTO" in sql
        assert "ON CONFLICT" in sql

    async def test_write_upsert_same_key(self, connector, _mock_conn, _mock_litellm):
        await connector.write("doc1", {"v": 1}, MemoryScope.SHARED, target="__shared__")
        await connector.write("doc1", {"v": 2}, MemoryScope.SHARED, target="__shared__")
        # Both writes should succeed (upsert)
        assert _mock_litellm.aembedding.await_count == 2


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_returns_entry(self, connector, _mock_conn):
        row = _make_row()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=row)
        _mock_conn.execute = AsyncMock(return_value=cursor)

        entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")
        assert entry is not None
        assert isinstance(entry, MemoryEntry)
        assert entry.key == "doc1"
        assert entry.scope == MemoryScope.SHARED

    async def test_read_returns_none_for_missing(self, connector, _mock_conn):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        _mock_conn.execute = AsyncMock(return_value=cursor)

        entry = await connector.read("missing", MemoryScope.SHARED, target="__shared__")
        assert entry is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_ordered_results(self, connector, _mock_conn, _mock_litellm):
        rows = [
            _make_row(key="doc1", value={"text": "hello"}),
            _make_row(key="doc2", value={"text": "world"}),
        ]
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=rows)
        _mock_conn.execute = AsyncMock(return_value=cursor)

        results = await connector.search(
            {"text": "hello", "limit": 5}, MemoryScope.SHARED, target="__shared__"
        )
        assert len(results) == 2
        assert results[0].key == "doc1"
        assert results[1].key == "doc2"

        # Verify cosine similarity query was used
        sql_call = _mock_conn.execute.call_args_list[-1]
        sql = sql_call.args[0]
        assert "<=> " in sql or "<=>" in sql


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_entry(self, connector, _mock_conn):
        cursor = AsyncMock()
        cursor.rowcount = 1
        _mock_conn.execute = AsyncMock(return_value=cursor)

        await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
        sql_call = _mock_conn.execute.call_args_list[-1]
        sql = sql_call.args[0]
        assert "DELETE FROM" in sql

    async def test_delete_raises_key_error_if_not_found(self, connector, _mock_conn):
        cursor = AsyncMock()
        cursor.rowcount = 0
        _mock_conn.execute = AsyncMock(return_value=cursor)

        with pytest.raises(KeyError):
            await connector.delete("missing", MemoryScope.SHARED, target="__shared__")


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------


class TestEmbed:
    async def test_embed_calls_litellm(self, connector, _mock_litellm):
        result = await connector._embed("hello world")
        assert result == FAKE_EMBEDDING
        _mock_litellm.aembedding.assert_awaited_once_with(
            model="text-embedding-3-small",
            input=["hello world"],
        )


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    async def test_different_scopes_use_different_params(self, connector, _mock_conn):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        _mock_conn.execute = AsyncMock(return_value=cursor)

        await connector.read("doc1", MemoryScope.RUN, target="run-1")
        call1_params = _mock_conn.execute.call_args_list[-1].args[1]

        await connector.read("doc1", MemoryScope.THREAD, target="thread-1")
        call2_params = _mock_conn.execute.call_args_list[-1].args[1]

        # Scope and target params differ
        assert call1_params[1] != call2_params[1]  # scope differs
        assert call1_params[2] != call2_params[2]  # target differs


# ---------------------------------------------------------------------------
# Live integration test stub
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestPgvectorLive:
    """Integration tests requiring a real Postgres+pgvector instance.

    Run with: pytest -m live tests/memory/test_pgvector.py
    Requires: testcontainers with pgvector/pgvector:pg16 image
    """

    async def test_roundtrip(self):
        """Vector write/read/semantic-search/delete against real Postgres+pgvector.

        DSN comes from ``ZEROTH_TEST_PGVECTOR_DSN`` (default
        ``postgresql://postgres:test@localhost:5432/postgres``). Embeddings are
        generated live via litellm, so ``OPENAI_API_KEY`` must be set. Skips if
        either the database is unreachable or no key is present.
        """
        import os

        import psycopg

        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("pgvector live test needs OPENAI_API_KEY for embeddings")

        dsn = os.environ.get(
            "ZEROTH_TEST_PGVECTOR_DSN", "postgresql://postgres:test@localhost:5432/postgres"
        )
        # The connector registers the pgvector type before issuing its own
        # CREATE EXTENSION, so the extension must already exist. Seed it (and
        # probe reachability) on a throwaway connection first.
        try:
            seed = await psycopg.AsyncConnection.connect(dsn, connect_timeout=10)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Postgres not reachable at {dsn}: {exc}")
        try:
            await seed.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await seed.commit()
        finally:
            await seed.close()

        # The DSN form, not an injected factory: after A07-12 the connector
        # disposes only of connections it created itself, so a factory that
        # mints a fresh connection per call would leak one per operation here.
        connector = PgvectorMemoryConnector(dsn, table_name="zeroth_test_vectors")
        try:
            await connector.write(
                "sky", {"text": "the sky is blue"}, MemoryScope.SHARED, target="__shared__"
            )
            await connector.write(
                "fruit", {"text": "bananas are yellow"}, MemoryScope.SHARED, target="__shared__"
            )

            entry = await connector.read("sky", MemoryScope.SHARED, target="__shared__")
            assert entry is not None
            assert entry.value == {"text": "the sky is blue"}

            # Cosine search ranks the semantically closest document first.
            hits = await connector.search(
                {"text": "what color is the sky", "limit": 2},
                MemoryScope.SHARED,
                target="__shared__",
            )
            assert hits and hits[0].key == "sky"

            await connector.delete("sky", MemoryScope.SHARED, target="__shared__")
            assert await connector.read("sky", MemoryScope.SHARED, target="__shared__") is None
            with pytest.raises(KeyError):
                await connector.delete("sky", MemoryScope.SHARED, target="__shared__")
        finally:
            cleanup = await psycopg.AsyncConnection.connect(dsn)
            try:
                await cleanup.execute("DROP TABLE IF EXISTS zeroth_test_vectors")
                await cleanup.commit()
            finally:
                await cleanup.close()


def test_row_to_entry_keeps_string_primitive_values():
    """Jsonb string primitives arrive pre-decoded; they must not be re-parsed."""
    from datetime import UTC, datetime

    from zeroth.integrations.memory.pgvector_connector import PgvectorMemoryConnector

    connector = PgvectorMemoryConnector.__new__(PgvectorMemoryConnector)
    now = datetime.now(UTC)
    entry = connector._row_to_entry(("k", "ok", "shared", "__shared__", {}, now, now))
    assert entry.value == "ok"
    entry = connector._row_to_entry(("k", '{"a": 1}', "shared", "__shared__", {}, now, now))
    assert entry.value == {"a": 1}


# ---------------------------------------------------------------------------
# Connection ownership (A07-12) and schema-setup concurrency (A07-24)
# ---------------------------------------------------------------------------


class _FakeAsyncConnection:
    """A psycopg-shaped connection whose ``__aexit__`` mirrors psycopg 3.3.3.

    The real one commits (or rolls back) and then closes -- but only
    ``if not self._pool``. Reproducing that exactly is the point: an
    ``AsyncMock`` with stubbed ``__aenter__``/``__aexit__`` never calls
    ``close()`` at all, so a "the connection stayed open" assertion would pass
    against the unfixed connector too.
    """

    def __init__(self, *, pool: object | None = None, rows: list[tuple] | None = None) -> None:
        self._pool = pool
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.aexit_calls = 0
        self._rows = rows if rows is not None else []

    async def execute(self, sql: str, params: list | None = None):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=self._rows[0] if self._rows else None)
        cursor.fetchall = AsyncMock(return_value=list(self._rows))
        cursor.rowcount = len(self._rows)
        return cursor

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> _FakeAsyncConnection:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.aexit_calls += 1
        if self.closed:
            return
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        if not self._pool:
            await self.close()


@pytest.fixture
def _no_vector_registration():
    """Keep ``register_vector_async`` away from the fake connection.

    It is patched for the whole test, not just construction: ``_get_conn``
    calls it on every operation, and the real one would drive type-catalog
    queries against a fake that cannot answer them.
    """
    with patch(
        "zeroth.integrations.memory.pgvector_connector.register_vector_async", new=AsyncMock()
    ) as patched:
        yield patched


def _connector_over(conn: _FakeAsyncConnection) -> PgvectorMemoryConnector:
    """Build a connector over an *injected* connection the caller owns."""
    connector = PgvectorMemoryConnector(
        conn_factory=AsyncMock(return_value=conn),
        table_name="test_vectors",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    connector._setup_done = True
    return connector


class TestInjectedConnectionOwnership:
    """A07-12 (narrowed): the connector owns only the connections it creates.

    The audit's original claim -- that ``async with conn`` closes pooled
    connections -- is refuted: psycopg 3.3.3 closes only ``if not self._pool``.
    What survives is narrower and real. A caller who injects a *plain*
    connection had it closed after a single operation, and every injected
    connection took a ``COMMIT`` this connector was never asked for, including
    after a pure read.
    """

    async def test_injected_connection_survives_a_read(self, _no_vector_registration) -> None:
        conn = _FakeAsyncConnection(rows=[_make_row()])
        connector = _connector_over(conn)

        entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert entry is not None
        assert conn.closed is False
        assert conn.aexit_calls == 0

    async def test_injected_connection_gets_no_commit_on_a_read(
        self, _no_vector_registration
    ) -> None:
        conn = _FakeAsyncConnection(rows=[_make_row()])
        connector = _connector_over(conn)

        await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert conn.commits == 0
        assert conn.rollbacks == 0

    async def test_injected_connection_gets_no_commit_on_a_search(
        self, _mock_litellm, _no_vector_registration
    ) -> None:
        conn = _FakeAsyncConnection(rows=[_make_row()])
        connector = _connector_over(conn)

        await connector.search({"text": "hi"}, MemoryScope.SHARED, target="__shared__")

        assert conn.commits == 0
        assert conn.closed is False

    async def test_injected_connection_still_commits_the_write_it_was_asked_for(
        self, _mock_litellm, _no_vector_registration
    ) -> None:
        """Durably storing what the caller asked to store is requested, not incidental."""
        conn = _FakeAsyncConnection()
        connector = _connector_over(conn)

        await connector.write("doc1", {"v": 1}, MemoryScope.SHARED, target="__shared__")

        assert conn.commits == 1
        assert conn.closed is False

    async def test_a_connection_the_connector_created_is_committed_and_closed(
        self, _no_vector_registration
    ) -> None:
        """The DSN path builds the connection, so it owns and disposes of it."""
        conn = _FakeAsyncConnection(rows=[_make_row()])
        with patch("zeroth.integrations.memory.pgvector_connector.psycopg") as mock_psycopg:
            mock_psycopg.AsyncConnection.connect = AsyncMock(return_value=conn)
            connector = PgvectorMemoryConnector(FAKE_CONNINFO, table_name="test_vectors")
            connector._setup_done = True

            await connector.read("doc1", MemoryScope.SHARED, target="__shared__")

        assert conn.aexit_calls == 1
        assert conn.closed is True


class TestSchemaSetupConcurrency:
    """A07-24: first use from two coroutines runs the schema DDL exactly once."""

    async def test_concurrent_first_use_runs_the_schema_setup_once(
        self, _no_vector_registration
    ) -> None:
        conn = _FakeAsyncConnection()
        connector = PgvectorMemoryConnector(
            conn_factory=AsyncMock(return_value=conn),
            table_name="test_vectors",
            embedding_dimensions=1536,
        )
        assert connector._setup_done is False

        entered = asyncio.Event()
        release = asyncio.Event()
        runs: list[object] = []

        async def fake_ensure_schema(connection: object) -> None:
            runs.append(connection)
            entered.set()
            # A real suspension point: without one, a stubbed coroutine returns
            # before the second caller ever runs, and the race cannot occur --
            # so the test would pass with or without the lock.
            await release.wait()

        connector._ensure_schema = fake_ensure_schema

        first = asyncio.create_task(connector._get_conn())
        await entered.wait()
        second = asyncio.create_task(connector._get_conn())
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        assert len(runs) == 1
        assert connector._setup_done is True

    async def test_schema_setup_is_skipped_once_it_has_run(self, _no_vector_registration) -> None:
        conn = _FakeAsyncConnection()
        connector = _connector_over(conn)
        runs: list[object] = []

        async def fake_ensure_schema(connection: object) -> None:
            runs.append(connection)

        connector._ensure_schema = fake_ensure_schema

        await connector._get_conn()

        assert runs == []

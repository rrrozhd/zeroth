"""Tests for PgvectorMemoryConnector.

Unit tests mock psycopg and litellm to test the connector logic
without requiring a real Postgres instance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from governai.memory.connector import MemoryConnector
from governai.memory.models import MemoryEntry, MemoryScope

from zeroth.core.memory.pgvector_connector import PgvectorMemoryConnector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536
FAKE_CONNINFO = "postgresql://test:test@localhost:5432/testdb"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_row(key="doc1", value={"text": "hello"}, scope="shared", target="__shared__"):
    """Build a fake DB row tuple matching the SELECT column order."""
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
    with patch("zeroth.core.memory.pgvector_connector.litellm") as mock_mod:
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
    with patch("zeroth.core.memory.pgvector_connector.register_vector_async", new=AsyncMock()):
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

        async def conn_factory():
            return await psycopg.AsyncConnection.connect(dsn)

        connector = PgvectorMemoryConnector(conn_factory, table_name="zeroth_test_vectors")
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
    """jsonb string primitives arrive pre-decoded; they must not be re-parsed."""
    from datetime import UTC, datetime

    from zeroth.core.memory.pgvector_connector import PgvectorMemoryConnector

    connector = PgvectorMemoryConnector.__new__(PgvectorMemoryConnector)
    now = datetime.now(UTC)
    entry = connector._row_to_entry(("k", "ok", "shared", "__shared__", {}, now, now))
    assert entry.value == "ok"
    entry = connector._row_to_entry(("k", '{"a": 1}', "shared", "__shared__", {}, now, now))
    assert entry.value == {"a": 1}

"""PgvectorMemoryConnector: async vector similarity search via pgvector.

Implements governed MemoryConnector protocol with HNSW-indexed cosine
similarity search. Embeddings are generated internally via litellm.

Per D-10, D-11, D-14 from Phase 14 planning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import litellm
import psycopg
from pgvector.psycopg import register_vector_async

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope

# Unquoted PostgreSQL identifiers: letter/underscore followed by word chars, max 63.
# Restricting to this subset lets us embed self._table directly in DDL/DML without
# quoting while rejecting anything that could carry a SQL injection payload.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class PgvectorMemoryConnector:
    """Memory connector backed by Postgres with pgvector extension.

    Uses HNSW index for fast approximate nearest-neighbor search with
    cosine similarity. Accepts an async connection factory (or a DSN
    string, which is wrapped into one) rather than managing connections
    directly.
    """

    connector_type = "pgvector"

    def __init__(
        self,
        conn_factory: Callable[[], Awaitable[psycopg.AsyncConnection]] | str,
        *,
        table_name: str = "zeroth_memory_vectors",
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ) -> None:
        # Resolve the embedding defaults lazily. Importing config.settings at module load
        # forms a circular import (settings ↔ pgvector_connector) that, depending on import
        # order, silently disabled this connector; deferring to call time avoids it entirely.
        from zeroth.platform.config.settings import (
            DEFAULT_EMBEDDING_DIMENSIONS,
            DEFAULT_EMBEDDING_MODEL,
        )

        if embedding_model is None:
            embedding_model = DEFAULT_EMBEDDING_MODEL
        if embedding_dimensions is None:
            embedding_dimensions = DEFAULT_EMBEDDING_DIMENSIONS
        if not _IDENT_RE.match(table_name):
            raise ValueError(
                f"invalid pgvector table_name {table_name!r}: must match {_IDENT_RE.pattern}"
            )
        # The connector owns only what it creates. A DSN string means it builds
        # the connection itself and is responsible for closing it; an injected
        # factory means the connection belongs to the caller, who may be
        # lending a long-lived one. See :meth:`_operation`.
        self._owns_connections = isinstance(conn_factory, str)
        if isinstance(conn_factory, str):
            dsn = conn_factory

            def _connect() -> Awaitable[psycopg.AsyncConnection]:
                return psycopg.AsyncConnection.connect(dsn)

            conn_factory = _connect
        self._conn_factory = conn_factory
        self._table = table_name
        self._embedding_model = embedding_model
        self._dimensions = embedding_dimensions
        self._setup_done = False
        # ``_setup_done`` was read and written across an await, so two
        # coroutines reaching first use together both saw False and both ran
        # the DDL. The lock plus the second check inside it collapses that to
        # one run; the check outside it keeps the steady state lock-free.
        self._schema_lock = asyncio.Lock()

    async def _get_conn(self) -> psycopg.AsyncConnection:
        """Obtain an async connection from the factory, register vector type."""
        conn = await self._conn_factory()
        await register_vector_async(conn)
        if not self._setup_done:
            async with self._schema_lock:
                if not self._setup_done:
                    await self._ensure_schema(conn)
                    self._setup_done = True
        return conn

    @contextlib.asynccontextmanager
    async def _operation(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Yield a connection for one operation, closing it only if owned.

        ``async with conn:`` on a psycopg connection does two things on exit:
        it commits (or rolls back), and -- since psycopg 3.3.3 closes only
        ``if not self._pool`` -- it closes any connection that did not come
        from a pool. Applied unconditionally that reached past what this
        connector owns twice over: a caller who lent a plain connection got it
        closed out from under them after a single read, and *every* injected
        connection received a ``COMMIT`` this connector was never asked for,
        including after pure reads.

        So the block is entered only for connections this connector created.
        For an injected connection the caller keeps transaction and lifecycle
        control; the deliberate ``commit()`` calls in ``write``/``delete``
        remain, because durably storing what the caller asked to store is the
        requested effect, not an incidental one.
        """
        conn = await self._get_conn()
        if not self._owns_connections:
            yield conn
            return
        async with conn:
            yield conn

    async def _ensure_schema(self, conn: psycopg.AsyncConnection) -> None:
        """Create the pgvector extension, table, and HNSW index if needed."""
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id SERIAL PRIMARY KEY,
                key TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_target TEXT NOT NULL,
                value JSONB NOT NULL,
                embedding vector({self._dimensions}) NOT NULL,
                metadata JSONB DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(key, scope, scope_target)
            )
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{self._table}_embedding
            ON {self._table} USING hnsw (embedding vector_cosine_ops)
        """)
        await conn.commit()

    async def _embed(self, text: str) -> list[float]:
        """Generate embedding vector via litellm."""
        response = await litellm.aembedding(
            model=self._embedding_model,
            input=[text],
        )
        return response.data[0]["embedding"]

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        """Look up a memory entry by key, scope, and target."""
        async with self._operation() as conn:
            cur = await conn.execute(
                f"SELECT key, value, scope, scope_target, metadata, created_at, updated_at "
                f"FROM {self._table} WHERE key = %s AND scope = %s AND scope_target = %s",
                [key, scope.value, target or ""],
            )
            row = await cur.fetchone()
            if not row:
                return None
            return self._row_to_entry(row)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Store a value with its embedding. Uses UPSERT for idempotent writes."""
        text_for_embedding = (
            f"{key}: {json.dumps(value)}" if isinstance(value, dict | list) else f"{key}: {value}"
        )
        embedding = await self._embed(text_for_embedding)
        async with self._operation() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (key, scope, scope_target, value, embedding)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (key, scope, scope_target)
                DO UPDATE SET value = EXCLUDED.value, embedding = EXCLUDED.embedding,
                             updated_at = NOW()
                """,
                [key, scope.value, target or "", json.dumps(value), embedding],
            )
            await conn.commit()

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        """Remove a memory entry. Raises KeyError if not found."""
        async with self._operation() as conn:
            cur = await conn.execute(
                f"DELETE FROM {self._table} WHERE key = %s AND scope = %s AND scope_target = %s",
                [key, scope.value, target or ""],
            )
            if cur.rowcount == 0:
                raise KeyError(key)
            await conn.commit()

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Semantic search using cosine similarity via pgvector HNSW index."""
        text = query.get("text", "")
        limit = query.get("limit", 10)
        embedding = await self._embed(text)
        async with self._operation() as conn:
            cur = await conn.execute(
                f"SELECT key, value, scope, scope_target, metadata, created_at, updated_at "
                f"FROM {self._table} "
                f"WHERE scope = %s AND scope_target = %s "
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                [scope.value, target or "", embedding, limit],
            )
            rows = await cur.fetchall()
            return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        """Convert a database row tuple to a MemoryEntry."""
        value = row[1]
        if isinstance(value, str):
            # psycopg already decodes jsonb, so a str here is either a raw
            # serialized payload (older drivers/TEXT columns) or a genuine
            # JSON string primitive like "ok" — only the former re-parses.
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        return MemoryEntry(
            key=row[0],
            value=value,
            scope=MemoryScope(row[2]),
            scope_target=row[3],
            metadata=row[4] or {},
            created_at=row[5],
            updated_at=row[6],
        )

"""PgvectorMemoryConnector: async vector similarity search via pgvector.

Implements GovernAI MemoryConnector protocol with HNSW-indexed cosine
similarity search. Embeddings are generated internally via litellm.

Per D-10, D-11, D-14 from Phase 14 planning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import litellm
import psycopg
from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue
from pgvector.psycopg import register_vector_async

from zeroth.core.config.settings import DEFAULT_EMBEDDING_DIMENSIONS, DEFAULT_EMBEDDING_MODEL

# Unquoted PostgreSQL identifiers: letter/underscore followed by word chars, max 63.
# Restricting to this subset lets us embed self._table directly in DDL/DML without
# quoting while rejecting anything that could carry a SQL injection payload.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class PgvectorMemoryConnector:
    """Memory connector backed by Postgres with pgvector extension.

    Uses HNSW index for fast approximate nearest-neighbor search with
    cosine similarity. Accepts an async connection factory rather than
    managing connections directly.
    """

    connector_type = "pgvector"

    def __init__(
        self,
        conn_factory: Callable[[], Awaitable[psycopg.AsyncConnection]],
        *,
        table_name: str = "zeroth_memory_vectors",
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        if not _IDENT_RE.match(table_name):
            raise ValueError(
                f"invalid pgvector table_name {table_name!r}: must match {_IDENT_RE.pattern}"
            )
        self._conn_factory = conn_factory
        self._table = table_name
        self._embedding_model = embedding_model
        self._dimensions = embedding_dimensions
        self._setup_done = False

    async def _get_conn(self) -> psycopg.AsyncConnection:
        """Obtain an async connection from the factory, register vector type."""
        conn = await self._conn_factory()
        await register_vector_async(conn)
        if not self._setup_done:
            await self._ensure_schema(conn)
            self._setup_done = True
        return conn

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
        conn = await self._get_conn()
        async with conn:
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
        conn = await self._get_conn()
        async with conn:
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
        conn = await self._get_conn()
        async with conn:
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
        conn = await self._get_conn()
        async with conn:
            cur = await conn.execute(
                f"SELECT key, value, scope, scope_target, metadata, created_at, updated_at "
                f"FROM {self._table} "
                f"WHERE scope = %s AND scope_target = %s "
                f"ORDER BY embedding <=> %s LIMIT %s",
                [scope.value, target or "", embedding, limit],
            )
            rows = await cur.fetchall()
            return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        """Convert a database row tuple to a MemoryEntry."""
        value = row[1]
        if isinstance(value, str):
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

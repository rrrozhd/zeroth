"""ChromaDBMemoryConnector: vector similarity search via ChromaDB HTTP client.

Implements GovernAI MemoryConnector protocol using an external ChromaDB
server for vector storage and similarity search. Embeddings are generated
internally via litellm.

Per D-10, D-12, D-14 from Phase 14 planning.
"""

from __future__ import annotations

import json
from typing import Any

import chromadb
import litellm
from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue

from zeroth.core.config.settings import DEFAULT_EMBEDDING_MODEL


class ChromaDBMemoryConnector:
    """Memory connector backed by an external ChromaDB server.

    Uses ChromaDB's HTTP client to connect to a running ChromaDB instance.
    Each scope+target combination maps to a separate collection with
    cosine similarity configured.
    """

    connector_type = "chroma"

    def __init__(
        self,
        client: chromadb.HttpClient,
        *,
        collection_prefix: str = "zeroth_memory",
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self._client = client
        self._collection_prefix = collection_prefix
        self._embedding_model = embedding_model

    def _collection_name(self, scope: MemoryScope, target: str | None) -> str:
        """Build a sanitized collection name from scope and target."""
        safe_target = (target or "default").replace("-", "_").replace(":", "_")
        return f"{self._collection_prefix}_{scope.value}_{safe_target}"

    def _get_collection(self, scope: MemoryScope, target: str | None) -> Any:
        """Get or create a ChromaDB collection for this scope+target."""
        return self._client.get_or_create_collection(
            name=self._collection_name(scope, target),
            metadata={"hnsw:space": "cosine"},
        )

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
        """Look up a memory entry by key from the appropriate collection."""
        collection = self._get_collection(scope, target)
        result = collection.get(ids=[key], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        doc = result["documents"][0]
        meta = result["metadatas"][0]
        return MemoryEntry(
            key=key,
            value=json.loads(doc) if doc else None,
            scope=scope,
            scope_target=target or "",
            metadata=meta or {},
        )

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Store a value with its embedding in ChromaDB. Uses upsert for idempotent writes."""
        collection = self._get_collection(scope, target)
        text = (
            f"{key}: {json.dumps(value)}" if isinstance(value, dict | list) else f"{key}: {value}"
        )
        embedding = await self._embed(text)
        collection.upsert(
            ids=[key],
            documents=[json.dumps(value)],
            embeddings=[embedding],
            metadatas=[{"key": key, "scope": scope.value, "target": target or ""}],
        )

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Remove a memory entry. Raises KeyError if not found."""
        collection = self._get_collection(scope, target)
        existing = collection.get(ids=[key])
        if not existing["ids"]:
            raise KeyError(key)
        collection.delete(ids=[key])

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Semantic search using cosine similarity via ChromaDB."""
        collection = self._get_collection(scope, target)
        text = query.get("text", "")
        limit = query.get("limit", 10)
        embedding = await self._embed(text)
        results = collection.query(
            query_embeddings=[embedding],
            n_results=limit,
            include=["documents", "metadatas"],
        )
        entries = []
        for i, doc_id in enumerate(results["ids"][0]):
            doc = results["documents"][0][i]
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            entries.append(
                MemoryEntry(
                    key=doc_id,
                    value=json.loads(doc) if doc else None,
                    scope=scope,
                    scope_target=target or "",
                    metadata=meta or {},
                )
            )
        return entries

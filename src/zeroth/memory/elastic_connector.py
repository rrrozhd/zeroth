"""ElasticsearchMemoryConnector: full-text search via Elasticsearch async client.

Implements GovernAI MemoryConnector protocol using Elasticsearch for
full-text search capabilities. Does not require embedding generation --
relies on Elasticsearch's built-in text analysis.

Per D-10, D-13, D-14 from Phase 14 planning.
"""

from __future__ import annotations

import json
from typing import Any

from elasticsearch import AsyncElasticsearch, NotFoundError
from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue


class ElasticsearchMemoryConnector:
    """Memory connector backed by Elasticsearch.

    Uses Elasticsearch's full-text search for memory retrieval.
    Each scope+target combination maps to a separate index.
    """

    connector_type = "elasticsearch"

    def __init__(
        self,
        client: AsyncElasticsearch,
        *,
        index_prefix: str = "zeroth_memory",
    ) -> None:
        self._client = client
        self._index_prefix = index_prefix

    def _index_name(self, scope: MemoryScope, target: str | None) -> str:
        """Build a sanitized index name from scope and target."""
        safe_target = (target or "default").replace("-", "_").replace(":", "_").lower()
        return f"{self._index_prefix}_{scope.value}_{safe_target}"

    def _doc_id(self, key: str) -> str:
        """Map a memory key to an Elasticsearch document ID."""
        return key

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        """Look up a memory entry by key from the appropriate index."""
        index = self._index_name(scope, target)
        try:
            result = await self._client.get(index=index, id=self._doc_id(key))
            source = result["_source"]
            return MemoryEntry(
                key=key,
                value=source.get("value"),
                scope=scope,
                scope_target=target or "",
                metadata=source.get("metadata", {}),
            )
        except NotFoundError:
            return None

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Index a document in Elasticsearch."""
        index = self._index_name(scope, target)
        text = (
            f"{key}: {json.dumps(value)}" if isinstance(value, dict | list) else f"{key}: {value}"
        )
        doc: dict[str, Any] = {
            "key": key,
            "value": value,
            "scope": scope.value,
            "scope_target": target or "",
            "text": text,
            "metadata": {},
        }
        await self._client.index(index=index, id=self._doc_id(key), document=doc)

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Remove a document. Raises KeyError if not found."""
        index = self._index_name(scope, target)
        try:
            await self._client.delete(index=index, id=self._doc_id(key))
        except NotFoundError:
            raise KeyError(key)  # noqa: B904

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Full-text search using Elasticsearch match query."""
        index = self._index_name(scope, target)
        text = query.get("text", "")
        limit = query.get("limit", 10)
        body: dict[str, Any] = {
            "query": {"match": {"text": text}} if text else {"match_all": {}},
            "size": limit,
        }
        result = await self._client.search(index=index, body=body)
        entries = []
        for hit in result["hits"]["hits"]:
            source = hit["_source"]
            entries.append(
                MemoryEntry(
                    key=source["key"],
                    value=source.get("value"),
                    scope=scope,
                    scope_target=target or "",
                    metadata=source.get("metadata", {}),
                )
            )
        return entries

"""In-memory connector implementations for agent memory.

Each connector stores key-value data using the governed async MemoryConnector
protocol. Storage layout mirrors the governed runtime's DictMemoryConnector:
_store[scope.value][target][key] = MemoryEntry
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.core.governed.memory.models import MemoryEntry, MemoryScope


def _utcnow() -> datetime:
    return datetime.now(UTC)


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


class RunEphemeralMemoryConnector:
    """Memory that only lasts for a single run and is thrown away afterward.

    Use this when an agent needs scratch space during one execution but
    the data doesn't need to survive after the run finishes.
    """

    connector_type = "ephemeral"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict[str, MemoryEntry]]] = {}

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        entry = self._store.get(scope.value, {}).get(target or "", {}).get(key)
        if entry is None:
            return None
        return entry.model_copy(deep=True)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        scope_key = scope.value
        target_key = target or ""
        bucket = self._store.setdefault(scope_key, {}).setdefault(target_key, {})
        existing = bucket.get(key)
        if existing is not None:
            bucket[key] = existing.model_copy(
                update={"value": value, "updated_at": _utcnow()}, deep=True
            )
        else:
            bucket[key] = MemoryEntry(key=key, value=value, scope=scope, scope_target=target_key)

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        if key not in bucket:
            raise KeyError(key)
        del bucket[key]

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        text = query.get("text", "").lower()
        results = []
        for entry in bucket.values():
            if not text or text in entry.key.lower() or text in str(entry.value).lower():
                results.append(entry.model_copy(deep=True))
        return results


class KeyValueMemoryConnector:
    """Simple key-value memory connector.

    A general-purpose connector for storing and retrieving data by key.
    """

    connector_type = "key_value"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict[str, MemoryEntry]]] = {}

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        entry = self._store.get(scope.value, {}).get(target or "", {}).get(key)
        if entry is None:
            return None
        return entry.model_copy(deep=True)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        scope_key = scope.value
        target_key = target or ""
        bucket = self._store.setdefault(scope_key, {}).setdefault(target_key, {})
        existing = bucket.get(key)
        if existing is not None:
            bucket[key] = existing.model_copy(
                update={"value": value, "updated_at": _utcnow()}, deep=True
            )
        else:
            bucket[key] = MemoryEntry(key=key, value=value, scope=scope, scope_target=target_key)

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        if key not in bucket:
            raise KeyError(key)
        del bucket[key]

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Rank entries by token overlap with the query text.

        Whole-phrase substring matches outrank token overlap; an empty query
        returns everything. Honors ``query["limit"]`` (the retrieval node's
        ``top_k``). Sentence queries — the shape a RetrievalNode produces —
        therefore work against this connector without a vector backend.
        """
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        text = str(query.get("text", "")).lower()
        tokens = {token for token in _TOKEN_SPLIT.split(text) if len(token) > 2}
        scored: list[tuple[int, MemoryEntry]] = []
        for entry in bucket.values():
            haystack = f"{entry.key} {entry.value}".lower()
            if not text:
                scored.append((0, entry))
                continue
            if text in haystack:
                score = len(tokens) + 1
            else:
                score = sum(1 for token in tokens if token in haystack)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = [entry.model_copy(deep=True) for _, entry in scored]
        limit = query.get("limit")
        if isinstance(limit, int) and limit > 0:
            results = results[:limit]
        return results


class ThreadMemoryConnector:
    """Memory scoped to a conversation thread.

    Use this when data should persist across multiple runs within the
    same thread (like a conversation) but stay separate between threads.
    """

    connector_type = "thread"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict[str, MemoryEntry]]] = {}

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        entry = self._store.get(scope.value, {}).get(target or "", {}).get(key)
        if entry is None:
            return None
        return entry.model_copy(deep=True)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        scope_key = scope.value
        target_key = target or ""
        bucket = self._store.setdefault(scope_key, {}).setdefault(target_key, {})
        existing = bucket.get(key)
        if existing is not None:
            bucket[key] = existing.model_copy(
                update={"value": value, "updated_at": _utcnow()}, deep=True
            )
        else:
            bucket[key] = MemoryEntry(key=key, value=value, scope=scope, scope_target=target_key)

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        if key not in bucket:
            raise KeyError(key)
        del bucket[key]

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        bucket = self._store.get(scope.value, {}).get(target or "", {})
        text = query.get("text", "").lower()
        results = []
        for entry in bucket.values():
            if not text or text in entry.key.lower() or text in str(entry.value).lower():
                results.append(entry.model_copy(deep=True))
        return results

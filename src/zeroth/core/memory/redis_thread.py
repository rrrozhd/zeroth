"""Redis-backed thread/conversation memory connector.

Implements the GovernAI MemoryConnector protocol using Redis sorted sets
(ZADD/ZREVRANGE) to maintain ordered conversation history. Each write
appends a new entry with a timestamp score, and reads return the most
recent entry.

Key format: ``{prefix}:{scope}:{target}:{key}``
Default prefix: ``zeroth:mem:thread`` (distinct from KV's ``zeroth:mem:kv``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue

if TYPE_CHECKING:
    import redis.asyncio as aioredis


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisThreadMemoryConnector:
    """Conversation-history memory backed by Redis sorted sets.

    Each ``write`` appends a new ``MemoryEntry`` to a sorted set keyed by
    timestamp, preserving the full conversation history. ``read`` returns
    the most recent entry.

    Conforms to the GovernAI ``MemoryConnector`` runtime-checkable protocol.
    """

    connector_type = "redis_thread"

    def __init__(
        self,
        redis_client: aioredis.Redis,
        *,
        key_prefix: str = "zeroth:mem:thread",
    ) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _key(self, key: str, scope: MemoryScope, target: str | None) -> str:
        """Build the full Redis key: ``{prefix}:{scope}:{target}:{key}``."""
        return f"{self._prefix}:{scope.value}:{target or ''}:{key}"

    # ------------------------------------------------------------------
    # MemoryConnector protocol
    # ------------------------------------------------------------------

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        """Return the most recent entry for *key*, or ``None`` if empty."""
        sorted_key = self._key(key, scope, target)
        items = await self._redis.zrevrange(sorted_key, 0, 0)
        if not items:
            return None
        return MemoryEntry.model_validate_json(items[0])

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Append a new entry to the sorted set with a timestamp score."""
        sorted_key = self._key(key, scope, target)
        entry = MemoryEntry(
            key=key,
            value=value,
            scope=scope,
            scope_target=target or "",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        score = time.time()
        await self._redis.zadd(sorted_key, {entry.model_dump_json(): score})

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Remove the entire sorted set for *key*. Raises ``KeyError`` if absent."""
        sorted_key = self._key(key, scope, target)
        deleted = await self._redis.delete(sorted_key)
        if not deleted:
            raise KeyError(key)

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Search thread entries, optionally filtered by text and limited.

        Supported query fields:
        - ``text``: substring match against entry values
        - ``limit``: maximum number of entries to return (default 100)
        """
        limit = query.get("limit", 100)
        text = query.get("text", "").lower()
        pattern = f"{self._prefix}:{scope.value}:{target or ''}:*"

        results: list[MemoryEntry] = []
        async for redis_key in self._redis.scan_iter(match=pattern):
            items = await self._redis.zrevrange(redis_key, 0, limit - 1)
            for raw in items:
                entry = MemoryEntry.model_validate_json(raw)
                if not text or text in str(entry.value).lower():
                    results.append(entry)
        return results[:limit]

"""Redis-backed thread/conversation memory connector.

Implements the governed MemoryConnector protocol using Redis sorted sets
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

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope

if TYPE_CHECKING:
    import redis.asyncio as aioredis


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisThreadMemoryConnector:
    """Conversation-history memory backed by Redis sorted sets.

    Each ``write`` appends a new ``MemoryEntry`` to a sorted set keyed by
    timestamp, preserving the full conversation history. ``read`` returns
    the most recent entry.

    Conforms to the governed ``MemoryConnector`` runtime-checkable protocol.
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

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        """Remove the entire sorted set for *key*. Raises ``KeyError`` if absent."""
        sorted_key = self._key(key, scope, target)
        deleted = await self._redis.delete(sorted_key)
        if not deleted:
            raise KeyError(key)

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Search thread entries, optionally filtered by text and limited.

        Returns the ``limit`` most recent matching entries across every key in
        scope, newest first.

        Each key is its own sorted set, so a per-key ``zrevrange`` is ordered
        only *within* that key. Appending those runs in ``scan_iter`` order and
        slicing the result kept the promised count but handed back an arbitrary
        cross-key subset -- with two keys and ``limit=10``, ten entries from
        whichever key Redis happened to scan first, and nothing from the other
        however recent it was. Scores are therefore pulled alongside the
        members and the merge is ordered by score before it is truncated.

        Supported query fields:
        - ``text``: substring match against entry values
        - ``limit``: maximum number of entries to return (default 100)

        One bound is inherited rather than fixed: each key is read only
        ``limit`` deep, so a ``text`` filter can under-fill. If a key's top
        ``limit`` entries all fail the filter while its next one would match,
        that match is not returned. Reading deeper would mean reading each set
        in full, which is what ``limit`` exists to prevent.

        Args:
            query: Query fields as described above.
            scope: Memory scope to search within.
            target: Scope target (thread id) to search within.

        Returns:
            Up to ``limit`` matching entries, most recent first.

        Raises:
            ValueError: ``limit`` is not a non-negative integer.

        """
        limit = self._search_limit(query.get("limit", 100))
        if limit == 0:
            # zrevrange(key, 0, -1) reads every member of every set, so the
            # old code answered "at most zero entries" by fetching all of them.
            return []
        text = query.get("text", "").lower()
        pattern = f"{self._prefix}:{scope.value}:{target or ''}:*"

        scored: list[tuple[float, MemoryEntry]] = []
        async for redis_key in self._redis.scan_iter(match=pattern):
            items = await self._redis.zrevrange(redis_key, 0, limit - 1, withscores=True)
            for raw, score in items:
                entry = MemoryEntry.model_validate_json(raw)
                if not text or text in str(entry.value).lower():
                    scored.append((float(score), entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    @staticmethod
    def _search_limit(limit: Any) -> int:
        """Validate the caller's ``limit`` as a non-negative entry count.

        Args:
            limit: The raw ``limit`` field from the query.

        Returns:
            The limit as an int.

        Raises:
            ValueError: ``limit`` is not a non-negative integer.

        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"search 'limit' must be a non-negative integer, got {limit!r}")
        return limit

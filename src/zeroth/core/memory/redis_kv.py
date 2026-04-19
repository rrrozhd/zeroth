"""Redis-backed key-value memory connector.

Implements the GovernAI MemoryConnector protocol using Redis GET/SET/DEL
operations. Each key is stored as a JSON-serialised MemoryEntry under a
namespaced Redis key: ``{prefix}:{scope}:{target}:{key}``.

This connector is suitable for simple key-value state that must survive
process restarts (unlike the in-memory DictMemoryConnector).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue

if TYPE_CHECKING:
    import redis.asyncio as aioredis


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisKVMemoryConnector:
    """Key-value memory backed by Redis GET/SET/DEL.

    Conforms to the GovernAI ``MemoryConnector`` runtime-checkable protocol.
    """

    connector_type = "redis_kv"

    def __init__(
        self,
        redis_client: aioredis.Redis,
        *,
        key_prefix: str = "zeroth:mem:kv",
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
        """Look up a value by key. Returns ``None`` if not found."""
        raw = await self._redis.get(self._key(key, scope, target))
        if raw is None:
            return None
        return MemoryEntry.model_validate_json(raw)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Store a value under the given key (upsert semantics)."""
        now = _utcnow()
        # Check for existing entry to preserve created_at
        existing_raw = await self._redis.get(self._key(key, scope, target))
        if existing_raw is not None:
            existing = MemoryEntry.model_validate_json(existing_raw)
            entry = existing.model_copy(update={"value": value, "updated_at": now})
        else:
            entry = MemoryEntry(
                key=key,
                value=value,
                scope=scope,
                scope_target=target or "",
                created_at=now,
                updated_at=now,
            )
        await self._redis.set(self._key(key, scope, target), entry.model_dump_json())

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Remove a key. Raises ``KeyError`` if it does not exist."""
        deleted = await self._redis.delete(self._key(key, scope, target))
        if not deleted:
            raise KeyError(key)

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Return entries matching *query* within the given scope/target.

        Supported query fields:
        - ``text``: substring match against key and value
        """
        pattern = f"{self._prefix}:{scope.value}:{target or ''}:*"
        text = query.get("text", "").lower()
        results: list[MemoryEntry] = []
        async for redis_key in self._redis.scan_iter(match=pattern):
            raw = await self._redis.get(redis_key)
            if raw:
                entry = MemoryEntry.model_validate_json(raw)
                if not text or text in entry.key.lower() or text in str(entry.value).lower():
                    results.append(entry)
        return results

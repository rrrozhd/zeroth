from __future__ import annotations

import json
from typing import Any

from zeroth.contracts.governed.models.audit import AuditEvent
from zeroth.governance.audit.emitter import AuditEmitter


class RedisAuditEmitter(AuditEmitter):
    """Redis-backed durable audit emitter."""

    def __init__(
        self,
        *,
        redis_url: str,
        prefix: str = "governai:audit",
        ttl_seconds: int | None = None,
        redis_client: Any | None = None,
    ) -> None:
        """Initialize RedisAuditEmitter."""
        self.redis_url = redis_url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds
        self._redis = redis_client

    async def _client(self) -> Any:
        """Internal helper to client."""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "RedisAuditEmitter requires 'redis' package (redis.asyncio)"
            ) from exc
        self._redis = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
        return self._redis

    def _run_key(self, run_id: str) -> str:
        """Internal helper to run key."""
        return f"{self.prefix}:run:{run_id}"

    async def emit(self, event: AuditEvent) -> None:
        """Emit."""
        client = await self._client()
        payload = event.model_dump_json()
        key = self._run_key(event.run_id)
        await client.rpush(key, payload)
        if self.ttl_seconds is not None:
            await client.expire(key, int(self.ttl_seconds))

    async def events_for_run(self, run_id: str) -> list[AuditEvent]:
        """Events for run."""
        client = await self._client()
        payloads = await client.lrange(self._run_key(run_id), 0, -1)
        out: list[AuditEvent] = []
        for payload in payloads:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            if not isinstance(payload, str):
                payload = json.dumps(payload)
            out.append(AuditEvent.model_validate_json(payload))
        return out

    async def aclose(self) -> None:
        """Aclose."""
        if self._redis is None:
            return
        close = getattr(self._redis, "aclose", None)
        if callable(close):
            await close()

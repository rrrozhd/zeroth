"""Real-Redis reconnect proofs for tenant-scoped memory connectors."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.redis_kv import RedisKVMemoryConnector
from zeroth.integrations.memory.redis_thread import RedisThreadMemoryConnector
from zeroth.integrations.memory.tenant_scoped import TenantScopedMemoryConnector


@pytest.mark.security_rc
@pytest.mark.parametrize(
    "connector_type",
    [RedisKVMemoryConnector, RedisThreadMemoryConnector],
    ids=("redis-kv", "redis-thread"),
)
async def test_security_rc_memory_same_key_isolated_after_client_reconnect(
    connector_type,
) -> None:
    redis_url = os.environ.get("ZEROTH_TEST_REDIS_URL")
    if redis_url is None:
        pytest.skip("ZEROTH_TEST_REDIS_URL is not configured")

    import redis.asyncio as aioredis

    prefix = f"zeroth:test:security:memory:{uuid4().hex}"
    first = aioredis.from_url(redis_url)
    second = aioredis.from_url(redis_url)
    try:
        # A configured but unavailable RC service is an error, never a skip.
        await first.ping()
        await second.ping()
        tenant_a = TenantScopedMemoryConnector(
            connector_type(first, key_prefix=prefix), tenant_id="tenant-a"
        )
        tenant_b = TenantScopedMemoryConnector(
            connector_type(second, key_prefix=prefix), tenant_id="tenant-b"
        )
        await tenant_a.write("same-key", {"owner": "A"}, MemoryScope.SHARED, target="same")
        assert await tenant_b.read("same-key", MemoryScope.SHARED, target="same") is None
        await tenant_b.write("same-key", {"owner": "B"}, MemoryScope.SHARED, target="same")
        assert (await tenant_a.read("same-key", MemoryScope.SHARED, target="same")).value == {
            "owner": "A"
        }
        assert (await tenant_b.read("same-key", MemoryScope.SHARED, target="same")).value == {
            "owner": "B"
        }
    finally:
        await first.aclose()
        await second.aclose()

    reconnected_a = aioredis.from_url(redis_url)
    reconnected_b = aioredis.from_url(redis_url)
    try:
        tenant_a = TenantScopedMemoryConnector(
            connector_type(reconnected_a, key_prefix=prefix), tenant_id="tenant-a"
        )
        tenant_b = TenantScopedMemoryConnector(
            connector_type(reconnected_b, key_prefix=prefix), tenant_id="tenant-b"
        )
        assert (await tenant_a.read("same-key", MemoryScope.SHARED, target="same")).value == {
            "owner": "A"
        }
        assert (await tenant_b.read("same-key", MemoryScope.SHARED, target="same")).value == {
            "owner": "B"
        }
        await tenant_b.delete("same-key", MemoryScope.SHARED, target="same")
        assert (await tenant_a.read("same-key", MemoryScope.SHARED, target="same")).value == {
            "owner": "A"
        }
    finally:
        async for key in reconnected_a.scan_iter(match=f"{prefix}:*"):
            await reconnected_a.delete(key)
        await reconnected_a.aclose()
        await reconnected_b.aclose()

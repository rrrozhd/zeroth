"""Real-Redis tenant artifact isolation release-candidate proof."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from zeroth.platform.artifacts.errors import ArtifactNotFoundError, ArtifactStorageError
from zeroth.platform.artifacts.store import RedisArtifactStore
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore


@pytest.mark.live
@pytest.mark.asyncio
async def test_security_rc_redis_artifact_isolation_survives_reconnect() -> None:
    """Two real clients and reconstructed wrappers retain physical isolation."""
    redis_url = os.environ.get("ZEROTH_TEST_REDIS_URL")
    if redis_url is None:
        pytest.skip("ZEROTH_TEST_REDIS_URL is not configured")

    import redis.asyncio as aioredis

    prefix = f"zeroth:test:security:artifact:{uuid4().hex}"
    first_client = aioredis.from_url(redis_url)
    second_client = aioredis.from_url(redis_url)
    try:
        # A configured but unreachable release service is a failure, never a skip.
        await first_client.ping()
        await second_client.ping()
        tenant_a = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=first_client),
            tenant_id="tenant-a",
            workspace_id=None,
        )
        tenant_b = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=second_client),
            tenant_id="tenant-b",
            workspace_id="workspace/*?[]\\",
        )
        key = "run*?[]\\/node/key"
        owner_only_key = "owner-only/node/key"
        await tenant_a.store(key, b"A", "application/octet-stream")
        await tenant_a.store(owner_only_key, b"owner-only", "application/octet-stream")
        await tenant_b.store(key, b"B", "application/octet-stream")
        assert await tenant_a.retrieve(key) == b"A"
        assert await tenant_b.retrieve(key) == b"B"
    finally:
        await first_client.aclose()
        await second_client.aclose()

    reconnected_a = aioredis.from_url(redis_url)
    reconnected_b = aioredis.from_url(redis_url)
    try:
        tenant_a = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=reconnected_a),
            tenant_id="tenant-a",
            workspace_id=None,
        )
        tenant_b = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=reconnected_b),
            tenant_id="tenant-b",
            workspace_id="workspace/*?[]\\",
        )
        assert await tenant_a.retrieve(key) == b"A"
        with pytest.raises(ArtifactNotFoundError):
            await tenant_b.retrieve(owner_only_key)
        assert await tenant_b.delete(owner_only_key, idempotency_key="foreign-delete") is False
        assert await tenant_a.retrieve(owner_only_key) == b"owner-only"

        await tenant_a.store("receipt-run/a", b"a", "application/octet-stream")
        await tenant_a.store("receipt-run/b", b"b", "application/octet-stream")
        assert await tenant_a.delete("receipt-run/a", idempotency_key="bound-receipt") is True
        assert await tenant_a.delete("receipt-run/a", idempotency_key="bound-receipt") is True
        with pytest.raises(ArtifactStorageError, match="reused for another operation") as misuse:
            await tenant_a.delete("receipt-run/b", idempotency_key="bound-receipt")
        assert "scopes/v1" not in str(misuse.value)
        with pytest.raises(ArtifactStorageError, match="reused for another operation"):
            await tenant_a.cleanup_run("receipt-run", idempotency_key="bound-receipt")
        assert await tenant_a.retrieve("receipt-run/b") == b"b"

        assert await tenant_a.cleanup_run("run*?[]\\", idempotency_key="cleanup") == 2
        assert await tenant_a.cleanup_run("run*?[]\\", idempotency_key="cleanup") == 2
        with pytest.raises(ArtifactStorageError, match="reused for another operation"):
            await tenant_a.cleanup_run("other-run", idempotency_key="cleanup")
        with pytest.raises(ArtifactNotFoundError):
            await tenant_a.retrieve(key)
        assert await tenant_b.retrieve(key) == b"B"
    finally:
        async for stale in reconnected_a.scan_iter(match=f"{prefix}:*"):
            await reconnected_a.delete(stale)
        await reconnected_a.aclose()
        await reconnected_b.aclose()

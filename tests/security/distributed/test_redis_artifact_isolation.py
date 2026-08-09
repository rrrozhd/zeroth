"""Real-Redis tenant artifact isolation release-candidate proof."""

from __future__ import annotations

import asyncio
import hashlib
import os
from uuid import uuid4

import pytest

from zeroth.platform.artifacts.errors import ArtifactNotFoundError, ArtifactStorageError
from zeroth.platform.artifacts.store import RedisArtifactStore
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore


@pytest.mark.security_rc
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
        await tenant_a.store("receipt-run/a", b"a", "application/octet-stream")
        await tenant_a.store("receipt-run/b", b"b", "application/octet-stream")
        assert await tenant_a.delete("receipt-run/a", idempotency_key="bound-receipt") is True
        await tenant_a.store("cleanup-restart/a", b"cleanup", "application/octet-stream")
        assert await tenant_a.cleanup_run("cleanup-restart", idempotency_key="cleanup-receipt") == 1
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

        # A reachable server-side WRONGTYPE error occurs before any mutation.
        await tenant_a.store("wrong-type/a", b"survives", "application/octet-stream")
        wrong_type_receipt = (
            f"{prefix}:erasure-receipt:v2:{tenant_a._receipt_id('wrong-type-receipt')}"
        )
        await reconnected_a.lpush(wrong_type_receipt, b"not-a-string-receipt")
        with pytest.raises(ArtifactStorageError, match="Redis erasure operation failed"):
            await tenant_a.delete("wrong-type/a", idempotency_key="wrong-type-receipt")
        assert await tenant_a.retrieve("wrong-type/a") == b"survives"
        await reconnected_a.delete(wrong_type_receipt)

        # Receipt bindings survive client and wrapper reconstruction.
        script_digest = hashlib.sha1(  # noqa: S324 - Redis requires a SHA-1 script digest
            RedisArtifactStore._ERASURE_SCRIPT.encode()
        ).hexdigest()
        assert await reconnected_a.script_exists(script_digest) == [True]
        await tenant_a.store("receipt-run/a", b"replacement", "application/octet-stream")
        assert await tenant_a.delete("receipt-run/a", idempotency_key="bound-receipt") is True
        assert await tenant_a.retrieve("receipt-run/a") == b"replacement"
        with pytest.raises(ArtifactStorageError, match="reused for another operation") as misuse:
            await tenant_a.delete("receipt-run/b", idempotency_key="bound-receipt")
        assert "scopes/v1" not in str(misuse.value)
        with pytest.raises(ArtifactStorageError, match="reused for another operation"):
            await tenant_a.cleanup_run("receipt-run", idempotency_key="bound-receipt")
        assert await tenant_a.retrieve("receipt-run/b") == b"b"
        await tenant_a.store(
            "cleanup-restart/replacement", b"replacement", "application/octet-stream"
        )
        assert await tenant_a.cleanup_run("cleanup-restart", idempotency_key="cleanup-receipt") == 1
        assert await tenant_a.retrieve("cleanup-restart/replacement") == b"replacement"
        with pytest.raises(ArtifactStorageError, match="reused for another operation"):
            await tenant_a.cleanup_run("other-cleanup", idempotency_key="cleanup-receipt")

        # Two independent real clients compete for one receipt binding.
        tenant_a_peer = TenantScopedArtifactStore(
            RedisArtifactStore("", prefix=prefix, client=reconnected_b),
            tenant_id="tenant-a",
            workspace_id=None,
        )
        await tenant_a.store("concurrent/a", b"a", "application/octet-stream")
        await tenant_a.store("concurrent/b", b"b", "application/octet-stream")
        competing = await asyncio.gather(
            tenant_a.delete("concurrent/a", idempotency_key="concurrent-receipt"),
            tenant_a_peer.cleanup_run("concurrent", idempotency_key="concurrent-receipt"),
            return_exceptions=True,
        )
        winners = [
            index for index, result in enumerate(competing) if not isinstance(result, Exception)
        ]
        losers = [result for result in competing if isinstance(result, Exception)]
        assert len(winners) == len(losers) == 1
        assert isinstance(losers[0], ArtifactStorageError)
        assert str(losers[0]) == "idempotency key reused for another operation"
        if winners[0] == 0:
            assert (
                await tenant_a.delete("concurrent/a", idempotency_key="concurrent-receipt") is True
            )
            assert await tenant_a.retrieve("concurrent/b") == b"b"
        else:
            assert (
                await tenant_a_peer.cleanup_run("concurrent", idempotency_key="concurrent-receipt")
                == competing[1]
            )

        assert await tenant_a.cleanup_run("run*?[]\\", idempotency_key="cleanup") == 1
        assert await tenant_a.cleanup_run("run*?[]\\", idempotency_key="cleanup") == 1
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

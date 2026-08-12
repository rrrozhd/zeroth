"""Async database-backed token-bucket rate limiter and quota enforcer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
    persistence_operation,
)


def guardrail_identity_key(
    kind: str,
    *,
    tenant_id: str,
    workspace_id: str | None,
    deployment_ref: str,
    subject: str | None,
) -> str:
    """Return an opaque, injective identity for one guardrail namespace."""
    canonical = json.dumps(
        ["zeroth.guardrail.identity.v1", kind, tenant_id, workspace_id, deployment_ref, subject],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"guardrail:{kind}:v1:{digest}"


class TokenBucketRateLimiter:
    """Per-key token bucket backed by an async database.

    Each bucket has a fixed capacity and refills at a configurable rate.
    ``check_and_consume`` atomically checks whether a token is available and,
    if so, deducts it.  Returns True on success, False when the bucket is empty.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: NullWorkspaceScopeContext | None = None,
    ) -> None:
        self.database = database
        self.scope_context = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if scope_context is None
            else scope_context
        )
        self._buckets = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.rate_limit_buckets", self.scope_context
        )

    @classmethod
    def scoped(
        cls, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext
    ) -> TokenBucketRateLimiter:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a NullWorkspaceScopeContext")
        return cls(database, scope_context)

    @persistence_operation(ResourceOperation.READ)
    async def get(self, bucket_key: str) -> dict[str, object] | None:
        return await self._buckets.select_one(where={"bucket_key": bucket_key})

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def check_and_consume(
        self,
        bucket_key: str,
        *,
        capacity: float = 10.0,
        refill_rate: float = 1.0,
    ) -> bool:
        """Attempt to consume one token from the named bucket.

        Args:
            bucket_key:   Unique key for the bucket (e.g. tenant:deployment).
            capacity:     Maximum number of tokens.
            refill_rate:  Tokens added per second.

        Returns:
            True if a token was consumed, False if the bucket is empty.
        """
        now = utc_now()
        now_iso = now.isoformat()
        # write_lock serializes the read-modify-write. Without it two concurrent
        # callers interleave between the SELECT and the UPDATE and both consume the
        # same token, letting N requests through a capacity-1 bucket (audit S4).
        async with self._buckets.transaction(write_lock=True) as buckets:
            row = await buckets.select_one(
                where={"bucket_key": bucket_key},
                columns=("token_count", "last_refill_at"),
                for_update=True,
            )
            if row is None:
                # Cold start: create the bucket full, then fall through to the
                # uniform refill+consume path (so the first request consumes one
                # token exactly like every later one). ON CONFLICT DO NOTHING so
                # concurrent first-requests can't collide on the UNIQUE key (a
                # plain INSERT raised IntegrityError -> 500); re-read the locked row.
                await buckets.insert_if_absent(
                    {
                        "bucket_key": bucket_key,
                        "token_count": capacity,
                        "last_refill_at": now_iso,
                        "capacity": capacity,
                        "refill_rate": refill_rate,
                    },
                    conflict_columns=("tenant_id", "bucket_key"),
                )
                row = await buckets.select_one(
                    where={"bucket_key": bucket_key},
                    columns=("token_count", "last_refill_at"),
                    for_update=True,
                )
            assert row is not None

            last_refill = datetime.fromisoformat(row["last_refill_at"])
            elapsed = max(0.0, (now - last_refill).total_seconds())
            refilled = min(capacity, row["token_count"] + elapsed * refill_rate)

            if refilled < 1.0:
                # Update tokens without consuming (no bucket should go negative).
                await buckets.update(
                    {"token_count": refilled, "last_refill_at": now_iso},
                    where={"bucket_key": bucket_key},
                )
                return False

            await buckets.update(
                {"token_count": refilled - 1.0, "last_refill_at": now_iso},
                where={"bucket_key": bucket_key},
            )
            return True


class QuotaEnforcer:
    """Per-key rolling-window quota enforcer backed by an async database.

    ``check_and_increment`` checks whether the counter for a given key is
    below the configured limit within the current window, and if so atomically
    increments it.  Returns True when within quota, False when exceeded.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: NullWorkspaceScopeContext | None = None,
    ) -> None:
        self.database = database
        self.scope_context = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if scope_context is None
            else scope_context
        )
        self._counters = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.quota_counters", self.scope_context
        )

    @classmethod
    def scoped(
        cls, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext
    ) -> QuotaEnforcer:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a NullWorkspaceScopeContext")
        return cls(database, scope_context)

    @persistence_operation(ResourceOperation.READ)
    async def get(self, counter_key: str) -> dict[str, object] | None:
        return await self._counters.select_one(where={"counter_key": counter_key})

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def check_and_increment(
        self,
        counter_key: str,
        *,
        limit: int,
        window_seconds: int = 86400,
    ) -> bool:
        """Check and conditionally increment a quota counter.

        Args:
            counter_key:     Unique key for the counter (e.g. tenant:daily).
            limit:           Maximum allowed increments in the window.
            window_seconds:  Duration of the rolling window in seconds.

        Returns:
            True if within quota (counter incremented), False if exhausted.
        """
        now = utc_now()
        now_iso = now.isoformat()
        # write_lock serializes the check-and-increment. Without it two concurrent
        # requests at value=limit-1 both pass the ceiling check and both increment,
        # silently overshooting the daily quota (audit S3).
        async with self._counters.transaction(write_lock=True) as counters:
            row = await counters.select_one(
                where={"counter_key": counter_key},
                columns=("value", "window_start", "window_seconds"),
                for_update=True,
            )
            if row is None:
                # Cold start: create the counter at zero, then fall through to the
                # uniform ceiling+increment path (so the first request is counted
                # and gated exactly like every later one). ON CONFLICT DO NOTHING so
                # concurrent first-requests can't collide on the UNIQUE key; re-read
                # the locked row.
                await counters.insert_if_absent(
                    {
                        "counter_key": counter_key,
                        "value": 0,
                        "window_start": now_iso,
                        "window_seconds": window_seconds,
                    },
                    conflict_columns=("tenant_id", "counter_key"),
                )
                row = await counters.select_one(
                    where={"counter_key": counter_key},
                    columns=("value", "window_start", "window_seconds"),
                    for_update=True,
                )
            assert row is not None

            window_start = datetime.fromisoformat(row["window_start"])
            if (now - window_start).total_seconds() > row["window_seconds"]:
                # Window expired: reset.
                await counters.update(
                    {"value": 1, "window_start": now_iso, "window_seconds": window_seconds},
                    where={"counter_key": counter_key},
                )
                return True

            if row["value"] >= limit:
                return False

            await counters.update(
                {"value": int(row["value"]) + 1}, where={"counter_key": counter_key}
            )
            return True

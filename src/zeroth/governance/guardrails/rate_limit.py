"""Async database-backed token-bucket rate limiter and quota enforcer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil

from zeroth.governance.guardrails.config import MAX_RATE_LIMIT_RETRY_AFTER_SECONDS
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
    persistence_operation,
)
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import named_isolation_probe, persistence_surface


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


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Atomic token-bucket decision with actionable retry telemetry."""

    allowed: bool
    remaining: float
    utilization: float
    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """Atomic rolling-quota decision with actionable retry telemetry."""

    allowed: bool
    remaining: int
    utilization: float
    retry_after_seconds: int


@persistence_surface(
    "service.rate_limit_buckets", probe=named_isolation_probe("_drive_rate_limit_buckets")
)
class TokenBucketRateLimiter:
    """Per-key token bucket backed by an async database.

    Each bucket has a fixed capacity and refills at a configurable rate.
    ``check_and_consume`` atomically checks whether a token is available and,
    if so, deducts it.  Returns True on success, False when the bucket is empty.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        self._bind(database, NullWorkspaceScopeContext.for_default_compatibility())

    def _bind(self, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext) -> None:
        self.database = database
        self.scope_context = scope_context
        self._buckets = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.rate_limit_buckets", self.scope_context
        )

    @classmethod
    def scoped(
        cls, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext
    ) -> TokenBucketRateLimiter:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a NullWorkspaceScopeContext")
        instance = cls.__new__(cls)
        instance._bind(database, scope_context)
        return instance

    @persistence_operation(ResourceOperation.READ)
    async def get(self, bucket_key: str) -> dict[str, object] | None:
        return await self._buckets.select_one(where={"bucket_key": bucket_key})

    @property
    def table(self) -> ScopedTable:
        """Return the structurally scoped table for a coordinated transaction."""
        return self._buckets

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
        return (
            await self.decide(
                bucket_key,
                capacity=capacity,
                refill_rate=refill_rate,
            )
        ).allowed

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def decide(
        self,
        bucket_key: str,
        *,
        capacity: float = 10.0,
        refill_rate: float = 1.0,
    ) -> RateLimitDecision:
        """Consume one token atomically and return remaining/retry telemetry."""
        async with self._buckets.transaction(write_lock=True) as buckets:
            return await self._decide_bound(
                buckets,
                bucket_key,
                capacity=capacity,
                refill_rate=refill_rate,
            )

    async def _decide_bound(
        self,
        buckets: BoundStructuredTable,
        bucket_key: str,
        *,
        capacity: float,
        refill_rate: float,
    ) -> RateLimitDecision:
        """Apply a token decision inside an existing coordinated transaction."""
        row, now = await _locked_bucket(buckets, bucket_key, capacity, refill_rate)
        last_refill = datetime.fromisoformat(str(row["last_refill_at"]))
        refill_at = max(now, last_refill)
        elapsed = (refill_at - last_refill).total_seconds()
        refilled = min(capacity, float(row["token_count"]) + elapsed * refill_rate)
        allowed = refilled >= 1
        remaining = refilled - 1 if allowed else refilled
        await buckets.update(
            {
                "token_count": remaining,
                "last_refill_at": refill_at.isoformat(),
                "capacity": capacity,
                "refill_rate": refill_rate,
            },
            where={"bucket_key": bucket_key},
        )
        retry = (
            0
            if allowed
            else max(
                1,
                ceil(
                    min(
                        MAX_RATE_LIMIT_RETRY_AFTER_SECONDS,
                        (1 - refilled) / refill_rate,
                    )
                ),
            )
            if refill_rate > 0
            else MAX_RATE_LIMIT_RETRY_AFTER_SECONDS
        )
        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            utilization=max(0, min(1, 1 - remaining / capacity)),
            retry_after_seconds=retry,
        )


@persistence_surface("service.quota_counters", probe=named_isolation_probe("_drive_quota_counters"))
class QuotaEnforcer:
    """Per-key rolling-window quota enforcer backed by an async database.

    ``check_and_increment`` checks whether the counter for a given key is
    below the configured limit within the current window, and if so atomically
    increments it.  Returns True when within quota, False when exceeded.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        self._bind(database, NullWorkspaceScopeContext.for_default_compatibility())

    def _bind(self, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext) -> None:
        self.database = database
        self.scope_context = scope_context
        self._counters = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.quota_counters", self.scope_context
        )

    @classmethod
    def scoped(
        cls, database: AsyncDatabase, scope_context: NullWorkspaceScopeContext
    ) -> QuotaEnforcer:
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a NullWorkspaceScopeContext")
        instance = cls.__new__(cls)
        instance._bind(database, scope_context)
        return instance

    @persistence_operation(ResourceOperation.READ)
    async def get(self, counter_key: str) -> dict[str, object] | None:
        return await self._counters.select_one(where={"counter_key": counter_key})

    @property
    def table(self) -> ScopedTable:
        """Return the structurally scoped table for a coordinated transaction."""
        return self._counters

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
        return (
            await self.decide(
                counter_key,
                limit=limit,
                window_seconds=window_seconds,
            )
        ).allowed

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def decide(
        self,
        counter_key: str,
        *,
        limit: int,
        window_seconds: int = 86400,
    ) -> QuotaDecision:
        """Increment a rolling quota atomically and return remaining/retry telemetry."""
        async with self._counters.transaction(write_lock=True) as counters:
            return await self._decide_bound(
                counters,
                counter_key,
                limit=limit,
                window_seconds=window_seconds,
            )

    async def _decide_bound(
        self,
        counters: BoundStructuredTable,
        counter_key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> QuotaDecision:
        """Apply a quota decision inside an existing coordinated transaction."""
        row, now = await _locked_quota(counters, counter_key, window_seconds)
        window_start = datetime.fromisoformat(str(row["window_start"]))
        elapsed = max(0.0, (now - window_start).total_seconds())
        if elapsed >= int(row["window_seconds"]):
            value = 0
            window_start = now
            elapsed = 0
        else:
            value = int(row["value"])
        allowed = value < limit
        next_value = value + 1 if allowed else value
        await counters.update(
            {
                "value": next_value,
                "window_start": window_start.isoformat(),
                "window_seconds": window_seconds,
            },
            where={"counter_key": counter_key},
        )
        retry = 0 if allowed else max(1, ceil(window_seconds - elapsed))
        return QuotaDecision(
            allowed=allowed,
            remaining=max(0, limit - next_value),
            utilization=max(0, min(1, next_value / limit)),
            retry_after_seconds=retry,
        )


async def _locked_bucket(
    buckets: BoundStructuredTable,
    bucket_key: str,
    capacity: float,
    refill_rate: float,
) -> tuple[dict[str, object], datetime]:
    row = await buckets.select_one(where={"bucket_key": bucket_key}, for_update=True)
    if row is None:
        now = await buckets._database_now()  # noqa: SLF001 - bound transaction clock
        inserted = await buckets.insert_if_absent(
            {
                "bucket_key": bucket_key,
                "token_count": capacity,
                "last_refill_at": now.isoformat(),
                "capacity": capacity,
                "refill_rate": refill_rate,
            },
            conflict_columns=("tenant_id", "bucket_key"),
        )
        if inserted:
            return {
                "token_count": capacity,
                "last_refill_at": now.isoformat(),
            }, now
        row = await buckets.select_one(where={"bucket_key": bucket_key}, for_update=True)
    assert row is not None
    return row, await buckets._database_now()  # noqa: SLF001 - bound transaction clock


async def _locked_quota(
    counters: BoundStructuredTable,
    counter_key: str,
    window_seconds: int,
) -> tuple[dict[str, object], datetime]:
    row = await counters.select_one(where={"counter_key": counter_key}, for_update=True)
    if row is None:
        now = await counters._database_now()  # noqa: SLF001 - bound transaction clock
        inserted = await counters.insert_if_absent(
            {
                "counter_key": counter_key,
                "value": 0,
                "window_start": now.isoformat(),
                "window_seconds": window_seconds,
            },
            conflict_columns=("tenant_id", "counter_key"),
        )
        if inserted:
            return {
                "value": 0,
                "window_start": now.isoformat(),
                "window_seconds": window_seconds,
            }, now
        row = await counters.select_one(where={"counter_key": counter_key}, for_update=True)
    assert row is not None
    return row, await counters._database_now()  # noqa: SLF001 - bound transaction clock


# These two constructors predate postponed annotations and their immutable public
# signatures therefore expose the real ``None`` singleton rather than the string
# produced by ``from __future__ import annotations``. Preserve that exact surface
# while keeping the module's annotations postponed everywhere else.
TokenBucketRateLimiter.__init__.__annotations__["return"] = None
QuotaEnforcer.__init__.__annotations__["return"] = None

"""Async database-backed token-bucket rate limiter and quota enforcer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase


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


def _for_update(backend: str) -> str:
    """Row-lock suffix for the read side of a check-and-update.

    ``write_lock=True`` gives SQLite an exclusive BEGIN IMMEDIATE, but on
    PostgreSQL it only sets ``lock_timeout`` — the row itself must be locked with
    ``SELECT ... FOR UPDATE`` (mirrors ``coordination.ensure_and_lock_row``).
    """
    return " FOR UPDATE" if backend == "postgres" else ""


@dataclass(slots=True)
class TokenBucketRateLimiter:
    """Per-key token bucket backed by an async database.

    Each bucket has a fixed capacity and refills at a configurable rate.
    ``check_and_consume`` atomically checks whether a token is available and,
    if so, deducts it.  Returns True on success, False when the bucket is empty.
    """

    database: AsyncDatabase

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
        lock = _for_update(self.database.backend)
        # write_lock serializes the read-modify-write. Without it two concurrent
        # callers interleave between the SELECT and the UPDATE and both consume the
        # same token, letting N requests through a capacity-1 bucket (audit S4).
        async with self.database.transaction(write_lock=True) as conn:
            row = await conn.fetch_one(
                "SELECT token_count, last_refill_at FROM rate_limit_buckets "
                f"WHERE bucket_key = ?{lock}",
                (bucket_key,),
            )
            if row is None:
                # Cold start: create the bucket full, then fall through to the
                # uniform refill+consume path (so the first request consumes one
                # token exactly like every later one). ON CONFLICT DO NOTHING so
                # concurrent first-requests can't collide on the UNIQUE key (a
                # plain INSERT raised IntegrityError -> 500); re-read the locked row.
                await conn.execute(
                    """
                    INSERT INTO rate_limit_buckets
                        (bucket_key, token_count, last_refill_at, capacity, refill_rate)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bucket_key) DO NOTHING
                    """,
                    (bucket_key, capacity, now_iso, capacity, refill_rate),
                )
                row = await conn.fetch_one(
                    "SELECT token_count, last_refill_at FROM rate_limit_buckets "
                    f"WHERE bucket_key = ?{lock}",
                    (bucket_key,),
                )

            last_refill = datetime.fromisoformat(row["last_refill_at"])
            elapsed = max(0.0, (now - last_refill).total_seconds())
            refilled = min(capacity, row["token_count"] + elapsed * refill_rate)

            if refilled < 1.0:
                # Update tokens without consuming (no bucket should go negative).
                await conn.execute(
                    "UPDATE rate_limit_buckets"
                    " SET token_count = ?, last_refill_at = ? WHERE bucket_key = ?",
                    (refilled, now_iso, bucket_key),
                )
                return False

            await conn.execute(
                "UPDATE rate_limit_buckets"
                " SET token_count = ?, last_refill_at = ? WHERE bucket_key = ?",
                (refilled - 1.0, now_iso, bucket_key),
            )
            return True


@dataclass(slots=True)
class QuotaEnforcer:
    """Per-key rolling-window quota enforcer backed by an async database.

    ``check_and_increment`` checks whether the counter for a given key is
    below the configured limit within the current window, and if so atomically
    increments it.  Returns True when within quota, False when exceeded.
    """

    database: AsyncDatabase

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
        lock = _for_update(self.database.backend)
        # write_lock serializes the check-and-increment. Without it two concurrent
        # requests at value=limit-1 both pass the ceiling check and both increment,
        # silently overshooting the daily quota (audit S3).
        async with self.database.transaction(write_lock=True) as conn:
            row = await conn.fetch_one(
                "SELECT value, window_start, window_seconds"
                f" FROM quota_counters WHERE counter_key = ?{lock}",
                (counter_key,),
            )
            if row is None:
                # Cold start: create the counter at zero, then fall through to the
                # uniform ceiling+increment path (so the first request is counted
                # and gated exactly like every later one). ON CONFLICT DO NOTHING so
                # concurrent first-requests can't collide on the UNIQUE key; re-read
                # the locked row.
                await conn.execute(
                    "INSERT INTO quota_counters"
                    " (counter_key, value, window_start, window_seconds) VALUES (?, 0, ?, ?)"
                    " ON CONFLICT(counter_key) DO NOTHING",
                    (counter_key, now_iso, window_seconds),
                )
                row = await conn.fetch_one(
                    "SELECT value, window_start, window_seconds"
                    f" FROM quota_counters WHERE counter_key = ?{lock}",
                    (counter_key,),
                )

            window_start = datetime.fromisoformat(row["window_start"])
            if (now - window_start).total_seconds() > row["window_seconds"]:
                # Window expired: reset.
                await conn.execute(
                    "UPDATE quota_counters"
                    " SET value = 1, window_start = ?, window_seconds = ? WHERE counter_key = ?",
                    (now_iso, window_seconds, counter_key),
                )
                return True

            if row["value"] >= limit:
                return False

            await conn.execute(
                "UPDATE quota_counters SET value = value + 1 WHERE counter_key = ?",
                (counter_key,),
            )
            return True

"""SQLite-backed token-bucket rate limiter and quota enforcer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class TokenBucketRateLimiter:
    """Per-key token bucket backed by SQLite.

    Each bucket has a fixed capacity and refills at a configurable rate.
    ``check_and_consume`` atomically checks whether a token is available and,
    if so, deducts it.  Returns True on success, False when the bucket is empty.
    """

    database: object  # SQLiteDatabase

    def check_and_consume(
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
        now = _utc_now()
        now_iso = now.isoformat()
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT token_count, last_refill_at FROM rate_limit_buckets WHERE bucket_key = ?",
                (bucket_key,),
            ).fetchone()
            if row is None:
                # First request: start full minus one consumed token.
                new_count = capacity - 1.0
                conn.execute(
                    """
                    INSERT INTO rate_limit_buckets
                        (bucket_key, token_count, last_refill_at, capacity, refill_rate)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (bucket_key, max(0.0, new_count), now_iso, capacity, refill_rate),
                )
                return True

            last_refill = datetime.fromisoformat(row["last_refill_at"])
            elapsed = max(0.0, (now - last_refill).total_seconds())
            refilled = min(capacity, row["token_count"] + elapsed * refill_rate)

            if refilled < 1.0:
                # Update tokens without consuming (no bucket should go negative).
                conn.execute(
                    "UPDATE rate_limit_buckets"
                    " SET token_count = ?, last_refill_at = ? WHERE bucket_key = ?",
                    (refilled, now_iso, bucket_key),
                )
                return False

            conn.execute(
                "UPDATE rate_limit_buckets"
                " SET token_count = ?, last_refill_at = ? WHERE bucket_key = ?",
                (refilled - 1.0, now_iso, bucket_key),
            )
            return True


@dataclass(slots=True)
class QuotaEnforcer:
    """Per-key rolling-window quota enforcer backed by SQLite.

    ``check_and_increment`` checks whether the counter for a given key is
    below the configured limit within the current window, and if so atomically
    increments it.  Returns True when within quota, False when exceeded.
    """

    database: object  # SQLiteDatabase

    def check_and_increment(
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
        now = _utc_now()
        now_iso = now.isoformat()
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT value, window_start, window_seconds"
                " FROM quota_counters WHERE counter_key = ?",
                (counter_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO quota_counters"
                    " (counter_key, value, window_start, window_seconds) VALUES (?, 1, ?, ?)",
                    (counter_key, now_iso, window_seconds),
                )
                return True

            window_start = datetime.fromisoformat(row["window_start"])
            if (now - window_start).total_seconds() > row["window_seconds"]:
                # Window expired: reset.
                conn.execute(
                    "UPDATE quota_counters"
                    " SET value = 1, window_start = ?, window_seconds = ? WHERE counter_key = ?",
                    (now_iso, window_seconds, counter_key),
                )
                return True

            if row["value"] >= limit:
                return False

            conn.execute(
                "UPDATE quota_counters SET value = value + 1 WHERE counter_key = ?",
                (counter_key,),
            )
            return True

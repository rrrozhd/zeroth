"""Tests for the async rate limiter and quota enforcer."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import zeroth.core.guardrails.rate_limit as rate_limit
from zeroth.core.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter

BUCKET = "tenant:default:deployment:test"


async def test_rate_limit_consumes_platform_clock(sqlite_db, monkeypatch) -> None:
    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(rate_limit, "utc_now", lambda: fixed)

    await TokenBucketRateLimiter(sqlite_db).check_and_consume("clock-bucket")
    await QuotaEnforcer(sqlite_db).check_and_increment("clock-quota", limit=5)

    async with sqlite_db.transaction() as connection:
        bucket = await connection.fetch_one(
            "SELECT last_refill_at FROM rate_limit_buckets WHERE bucket_key = ?",
            ("clock-bucket",),
        )
        quota = await connection.fetch_one(
            "SELECT window_start FROM quota_counters WHERE counter_key = ?",
            ("clock-quota",),
        )

    assert bucket is not None
    assert quota is not None
    assert bucket["last_refill_at"] == fixed.isoformat()
    assert quota["window_start"] == fixed.isoformat()


async def test_token_bucket_allows_first_request(sqlite_db) -> None:
    limiter = TokenBucketRateLimiter(sqlite_db)
    allowed = await limiter.check_and_consume(BUCKET, capacity=10.0, refill_rate=1.0)
    assert allowed is True


async def test_token_bucket_exhausts_after_capacity_requests(sqlite_db) -> None:
    limiter = TokenBucketRateLimiter(sqlite_db)
    capacity = 3

    for _ in range(capacity):
        allowed = await limiter.check_and_consume(BUCKET, capacity=float(capacity), refill_rate=1.0)
        assert allowed is True

    # Next request should be rejected.
    rejected = await limiter.check_and_consume(BUCKET, capacity=float(capacity), refill_rate=1.0)
    assert rejected is False


async def test_token_bucket_different_keys_are_independent(sqlite_db) -> None:
    limiter = TokenBucketRateLimiter(sqlite_db)
    bucket_a = "tenant:a"
    bucket_b = "tenant:b"

    # Exhaust bucket_a with capacity=1. Refill rate must be zero: at 100
    # tokens/s, any >10ms scheduling gap between the two calls refills the
    # bucket and the rejection assertion flakes under machine load.
    await limiter.check_and_consume(bucket_a, capacity=1.0, refill_rate=0.0)
    rejected = await limiter.check_and_consume(bucket_a, capacity=1.0, refill_rate=0.0)
    assert rejected is False

    # bucket_b is independent.
    allowed = await limiter.check_and_consume(bucket_b, capacity=1.0, refill_rate=0.0)
    assert allowed is True


async def test_quota_enforcer_allows_within_limit(sqlite_db) -> None:
    enforcer = QuotaEnforcer(sqlite_db)
    key = "tenant:default:daily"

    for _ in range(5):
        allowed = await enforcer.check_and_increment(key, limit=5, window_seconds=86400)
        assert allowed is True


async def test_quota_enforcer_rejects_after_limit(sqlite_db) -> None:
    enforcer = QuotaEnforcer(sqlite_db)
    key = "tenant:default:daily-limit"

    for _ in range(3):
        await enforcer.check_and_increment(key, limit=3, window_seconds=86400)

    rejected = await enforcer.check_and_increment(key, limit=3, window_seconds=86400)
    assert rejected is False


async def test_quota_enforcer_resets_after_window(sqlite_db) -> None:
    enforcer = QuotaEnforcer(sqlite_db)
    key = "tenant:default:short-window"

    # Exhaust a 1-second window.
    for _ in range(2):
        await enforcer.check_and_increment(key, limit=2, window_seconds=1)

    rejected = await enforcer.check_and_increment(key, limit=2, window_seconds=1)
    assert rejected is False

    # Wait for the window to expire.
    time.sleep(1.1)

    # Should be allowed again.
    allowed = await enforcer.check_and_increment(key, limit=2, window_seconds=1)
    assert allowed is True

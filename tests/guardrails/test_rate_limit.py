"""Tests for the SQLite-backed rate limiter and quota enforcer."""
from __future__ import annotations

import time

from zeroth.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.runs import RunRepository
from zeroth.storage import SQLiteDatabase


def _init_db(sqlite_db: SQLiteDatabase) -> SQLiteDatabase:
    """Ensure migrations (including guardrail tables) are applied."""
    RunRepository(sqlite_db)
    return sqlite_db

BUCKET = "tenant:default:deployment:test"


def test_token_bucket_allows_first_request(sqlite_db: SQLiteDatabase) -> None:
    limiter = TokenBucketRateLimiter(_init_db(sqlite_db))
    allowed = limiter.check_and_consume(BUCKET, capacity=10.0, refill_rate=1.0)
    assert allowed is True


def test_token_bucket_exhausts_after_capacity_requests(sqlite_db: SQLiteDatabase) -> None:
    limiter = TokenBucketRateLimiter(_init_db(sqlite_db))
    capacity = 3

    for _ in range(capacity):
        allowed = limiter.check_and_consume(BUCKET, capacity=float(capacity), refill_rate=1.0)
        assert allowed is True

    # Next request should be rejected.
    rejected = limiter.check_and_consume(BUCKET, capacity=float(capacity), refill_rate=1.0)
    assert rejected is False


def test_token_bucket_different_keys_are_independent(sqlite_db: SQLiteDatabase) -> None:
    limiter = TokenBucketRateLimiter(_init_db(sqlite_db))
    bucket_a = "tenant:a"
    bucket_b = "tenant:b"

    # Exhaust bucket_a with capacity=1.
    limiter.check_and_consume(bucket_a, capacity=1.0, refill_rate=100.0)
    rejected = limiter.check_and_consume(bucket_a, capacity=1.0, refill_rate=100.0)
    assert rejected is False

    # bucket_b is independent.
    allowed = limiter.check_and_consume(bucket_b, capacity=1.0, refill_rate=100.0)
    assert allowed is True


def test_quota_enforcer_allows_within_limit(sqlite_db: SQLiteDatabase) -> None:
    enforcer = QuotaEnforcer(_init_db(sqlite_db))
    key = "tenant:default:daily"

    for _ in range(5):
        allowed = enforcer.check_and_increment(key, limit=5, window_seconds=86400)
        assert allowed is True


def test_quota_enforcer_rejects_after_limit(sqlite_db: SQLiteDatabase) -> None:
    enforcer = QuotaEnforcer(_init_db(sqlite_db))
    key = "tenant:default:daily-limit"

    for _ in range(3):
        enforcer.check_and_increment(key, limit=3, window_seconds=86400)

    rejected = enforcer.check_and_increment(key, limit=3, window_seconds=86400)
    assert rejected is False


def test_quota_enforcer_resets_after_window(sqlite_db: SQLiteDatabase) -> None:
    enforcer = QuotaEnforcer(_init_db(sqlite_db))
    key = "tenant:default:short-window"

    # Exhaust a 1-second window.
    for _ in range(2):
        enforcer.check_and_increment(key, limit=2, window_seconds=1)

    rejected = enforcer.check_and_increment(key, limit=2, window_seconds=1)
    assert rejected is False

    # Wait for the window to expire.
    time.sleep(1.1)

    # Should be allowed again.
    allowed = enforcer.check_and_increment(key, limit=2, window_seconds=1)
    assert allowed is True

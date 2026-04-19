"""Tests for zeroth.core.http.circuit_breaker — CircuitBreaker, Registry, TokenBucket."""

from __future__ import annotations

import asyncio
import time

import pytest

from zeroth.core.http.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    InMemoryTokenBucket,
)
from zeroth.core.http.errors import CircuitOpenError


class TestCircuitState:
    """CircuitState enum values."""

    def test_values(self) -> None:
        assert CircuitState.CLOSED == "closed"
        assert CircuitState.OPEN == "open"
        assert CircuitState.HALF_OPEN == "half_open"


class TestCircuitBreaker:
    """CircuitBreaker state transitions."""

    @pytest.mark.asyncio
    async def test_starts_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_transitions_to_open_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60.0)
        for _ in range(3):
            await cb.record_failure()
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_raises_circuit_open_error(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=60.0)
        await cb.record_failure()
        with pytest.raises(CircuitOpenError):
            await cb.check()

    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        await asyncio.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_allows_probe(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        await cb.record_failure()
        await asyncio.sleep(0.1)
        # Should not raise -- allows one probe
        await cb.check()

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_success(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        await cb.record_failure()
        await asyncio.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        await cb.record_success()
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_to_open_on_failure(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        await cb.record_failure()
        await asyncio.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60.0)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        # After success, failure count should reset; one more failure shouldn't open
        await cb.record_failure()
        assert cb.state == CircuitState.CLOSED


class TestCircuitBreakerRegistry:
    """CircuitBreakerRegistry keyed lookup."""

    def test_returns_same_instance_for_same_key(self) -> None:
        reg = CircuitBreakerRegistry()
        cb1 = reg.get("api.example.com:443", failure_threshold=5, reset_timeout=30.0)
        cb2 = reg.get("api.example.com:443", failure_threshold=5, reset_timeout=30.0)
        assert cb1 is cb2

    def test_returns_different_instances_for_different_keys(self) -> None:
        reg = CircuitBreakerRegistry()
        cb1 = reg.get("api.example.com:443", failure_threshold=5, reset_timeout=30.0)
        cb2 = reg.get("other.example.com:443", failure_threshold=5, reset_timeout=30.0)
        assert cb1 is not cb2


class TestInMemoryTokenBucket:
    """InMemoryTokenBucket acquire and refill."""

    @pytest.mark.asyncio
    async def test_acquire_succeeds_within_burst(self) -> None:
        bucket = InMemoryTokenBucket(rate=10.0, burst=3)
        assert await bucket.acquire() is True
        assert await bucket.acquire() is True
        assert await bucket.acquire() is True

    @pytest.mark.asyncio
    async def test_acquire_fails_when_exhausted(self) -> None:
        bucket = InMemoryTokenBucket(rate=10.0, burst=2)
        await bucket.acquire()
        await bucket.acquire()
        assert await bucket.acquire() is False

    @pytest.mark.asyncio
    async def test_refills_over_time(self) -> None:
        bucket = InMemoryTokenBucket(rate=100.0, burst=1)
        await bucket.acquire()
        assert await bucket.acquire() is False
        # Wait for refill (100 tokens/sec = 1 token per 10ms)
        await asyncio.sleep(0.05)
        assert await bucket.acquire() is True

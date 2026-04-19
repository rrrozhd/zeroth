"""Circuit breaker, registry, and in-memory token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum

from zeroth.core.http.errors import CircuitOpenError


class CircuitState(StrEnum):
    """Possible states for a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-endpoint circuit breaker with CLOSED / OPEN / HALF_OPEN states.

    * **CLOSED** -- requests flow normally; failures are counted.
    * **OPEN** -- requests are rejected immediately after *failure_threshold*
      consecutive failures.  The breaker stays open for *reset_timeout* seconds.
    * **HALF_OPEN** -- after *reset_timeout* elapses, one probe request is
      allowed through.  Success transitions back to CLOSED; failure re-opens.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._failure_count = 0
        self._state = CircuitState.CLOSED
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    # -- public state property ------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Return the current effective state.

        If the breaker is OPEN and *reset_timeout* has elapsed, it
        dynamically transitions to HALF_OPEN.
        """
        if (
            self._state == CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self._reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    # -- mutation methods (always under lock) ----------------------------------

    async def record_success(self) -> None:
        """Record a successful call — resets the breaker to CLOSED."""
        async with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        """Record a failed call — may transition to OPEN."""
        async with self._lock:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    async def check(self) -> None:
        """Gate a request.  Raises :class:`CircuitOpenError` if OPEN.

        In HALF_OPEN state, one probe is allowed.
        """
        async with self._lock:
            current = self.state  # may auto-transition OPEN -> HALF_OPEN
            if current == CircuitState.OPEN:
                raise CircuitOpenError(f"{id(self)}")
            # HALF_OPEN: allow probe (caller must record_success/failure after)
            # CLOSED: allow freely


class CircuitBreakerRegistry:
    """Cache of per-endpoint :class:`CircuitBreaker` instances."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(
        self,
        endpoint_key: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
    ) -> CircuitBreaker:
        """Return an existing breaker or create a new one for *endpoint_key*."""
        if endpoint_key not in self._breakers:
            self._breakers[endpoint_key] = CircuitBreaker(
                failure_threshold=failure_threshold,
                reset_timeout=reset_timeout,
            )
        return self._breakers[endpoint_key]


class InMemoryTokenBucket:
    """Simple token-bucket rate limiter.

    Tokens refill at *rate* tokens per second up to a maximum of *burst*.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens: float = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Try to consume one token.  Returns ``True`` on success."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

"""Legacy import path for :mod:`zeroth.integrations.http.circuit_breaker`."""

from zeroth.integrations.http.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    InMemoryTokenBucket,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitState",
    "InMemoryTokenBucket",
]

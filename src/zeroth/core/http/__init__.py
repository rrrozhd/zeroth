"""Resilient HTTP client package.

Provides :class:`ResilientHttpClient` with automatic retry, exponential
backoff with jitter, per-endpoint circuit breaking, in-memory rate limiting,
connection pooling, and call-record auditing.
"""

from zeroth.core.http.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    InMemoryTokenBucket,
)
from zeroth.core.http.errors import (
    CircuitOpenError,
    HttpClientError,
    HttpRateLimitError,
    HttpRetryExhaustedError,
)
from zeroth.core.http.models import (
    AuthType,
    EndpointConfig,
    HttpCallRecord,
    HttpClientSettings,
    redact_url,
)

__all__ = [
    "AuthType",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitOpenError",
    "CircuitState",
    "EndpointConfig",
    "HttpCallRecord",
    "HttpClientError",
    "HttpClientSettings",
    "HttpRateLimitError",
    "HttpRetryExhaustedError",
    "InMemoryTokenBucket",
    "redact_url",
]

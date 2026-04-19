"""HTTP client error hierarchy."""

from __future__ import annotations


class HttpClientError(Exception):
    """Base error for all resilient-HTTP-client operations."""


class CircuitOpenError(HttpClientError):
    """Raised when the circuit breaker for an endpoint is open."""

    def __init__(self, endpoint_key: str) -> None:
        self.endpoint_key = endpoint_key
        super().__init__(f"Circuit breaker open for endpoint {endpoint_key}")


class HttpRetryExhaustedError(HttpClientError):
    """Raised after all retry attempts have been exhausted."""

    def __init__(self, attempts: int, last_error: str) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"All {attempts} retry attempts exhausted. Last error: {last_error}")


class HttpRateLimitError(HttpClientError):
    """Raised when the per-endpoint rate limit has been exceeded."""

    def __init__(self, endpoint_key: str) -> None:
        self.endpoint_key = endpoint_key
        super().__init__(f"Rate limit exceeded for endpoint {endpoint_key}")

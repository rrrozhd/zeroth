"""Legacy import path for :mod:`zeroth.integrations.http.errors`."""

from zeroth.integrations.http.errors import (
    CircuitOpenError,
    HttpClientError,
    HttpRateLimitError,
    HttpRetryExhaustedError,
)

__all__ = [
    "CircuitOpenError",
    "HttpClientError",
    "HttpRateLimitError",
    "HttpRetryExhaustedError",
]

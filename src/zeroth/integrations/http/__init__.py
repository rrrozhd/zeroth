"""Resilient HTTP client package.

Provides :class:`ResilientHttpClient` with automatic retry, exponential
backoff with jitter, per-endpoint circuit breaking, in-memory rate limiting,
connection pooling, and call-record auditing.

Every export resolves lazily, for the same reason as
:mod:`zeroth.econ.analytics`: ``zeroth.platform.config.settings``
historically needed only ``HttpClientSettings`` (defined in
:mod:`zeroth.platform.config.models` and republished from
:mod:`zeroth.integrations.http.models`), and eagerly importing the client
package pulled its dependencies into the platform layer.
``from zeroth.integrations.http import X`` is unchanged.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.integrations.http.circuit_breaker import (
        CircuitBreaker,
        CircuitBreakerRegistry,
        CircuitState,
        InMemoryTokenBucket,
    )
    from zeroth.integrations.http.client import ResilientHttpClient
    from zeroth.integrations.http.errors import (
        CircuitOpenError,
        HttpClientError,
        HttpRateLimitError,
        HttpRetryExhaustedError,
    )
    from zeroth.integrations.http.models import (
        AuthType,
        EndpointConfig,
        HttpCallRecord,
        HttpClientSettings,
        redact_url,
    )

_EXPORTS = {
    "CircuitBreaker": "circuit_breaker",
    "CircuitBreakerRegistry": "circuit_breaker",
    "CircuitState": "circuit_breaker",
    "InMemoryTokenBucket": "circuit_breaker",
    "ResilientHttpClient": "client",
    "CircuitOpenError": "errors",
    "HttpClientError": "errors",
    "HttpRateLimitError": "errors",
    "HttpRetryExhaustedError": "errors",
    "AuthType": "models",
    "EndpointConfig": "models",
    "HttpCallRecord": "models",
    "HttpClientSettings": "models",
    "redact_url": "models",
}

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
    "ResilientHttpClient",
    "redact_url",
]


def __getattr__(name: str) -> object:
    """Resolve a public HTTP symbol from its submodule on first access."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))

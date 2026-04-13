"""Resilient HTTP client with retry, circuit breaking, rate limiting, and audit.

Wraps :class:`httpx.AsyncClient` and layers on:

* Exponential backoff with jitter (D-05 formula)
* Per-endpoint circuit breaker (via :class:`CircuitBreakerRegistry`)
* In-memory token-bucket rate limiter
* Capability gating (set-based, no PolicyGuard dependency)
* Secret-resolved auth-header injection (via :class:`SecretProvider`)
* Call-record accumulation for audit logging
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from zeroth.core.http.circuit_breaker import CircuitBreakerRegistry, InMemoryTokenBucket
from zeroth.core.http.errors import (
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

if TYPE_CHECKING:
    from zeroth.core.policy.models import Capability
    from zeroth.core.secrets.provider import SecretProvider

logger = logging.getLogger(__name__)

# HTTP methods considered idempotent (safe to retry on status codes).
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


class ResilientHttpClient:
    """Production-grade HTTP client with resilience layers.

    Parameters
    ----------
    settings:
        Global :class:`HttpClientSettings` controlling retry, pool, timeouts, etc.
    secret_provider:
        Optional :class:`SecretProvider` for resolving auth secrets at call time.
    """

    def __init__(
        self,
        settings: HttpClientSettings,
        *,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        self._settings = settings
        self._secret_provider = secret_provider

        self._client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=settings.pool_max_connections,
                max_keepalive_connections=settings.pool_max_keepalive,
                keepalive_expiry=settings.pool_keepalive_expiry,
            ),
            timeout=httpx.Timeout(settings.default_timeout),
        )

        self._breaker_registry = CircuitBreakerRegistry()
        self._rate_limiters: dict[str, InMemoryTokenBucket] = {}
        self._call_records: list[HttpCallRecord] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_key(url: str) -> str:
        """Derive a per-endpoint key from *url* (``host:port``)."""
        parsed = urlparse(url)
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return f"{parsed.hostname}:{port}"

    def _get_rate_limiter(
        self,
        endpoint_key: str,
        config: EndpointConfig,
    ) -> InMemoryTokenBucket:
        """Return (or lazily create) the rate limiter for *endpoint_key*."""
        if endpoint_key not in self._rate_limiters:
            rate = config.rate_limit_rate or self._settings.default_rate_limit_rate
            burst = config.rate_limit_burst or self._settings.default_rate_limit_burst
            self._rate_limiters[endpoint_key] = InMemoryTokenBucket(
                rate=rate,
                burst=burst,
            )
        return self._rate_limiters[endpoint_key]

    def _resolve_config(self, endpoint_config: EndpointConfig | None) -> EndpointConfig:
        """Merge per-endpoint overrides with global defaults."""
        if endpoint_config is None:
            return EndpointConfig()
        return endpoint_config

    def _check_capabilities(
        self,
        method: str,
        effective_capabilities: set[Capability] | None,
    ) -> None:
        """Raise if required capabilities are missing."""
        if effective_capabilities is None:
            return  # no governance context — skip
        from zeroth.core.policy.models import Capability  # noqa: PLC0415

        read_caps = frozenset({Capability.NETWORK_READ, Capability.EXTERNAL_API_CALL})
        write_caps = frozenset({Capability.NETWORK_WRITE, Capability.EXTERNAL_API_CALL})
        required = read_caps if method.upper() in _IDEMPOTENT_METHODS else write_caps
        missing = required - effective_capabilities
        if missing:
            raise HttpClientError(
                f"Missing required capability for {method}: "
                f"{', '.join(sorted(str(c) for c in missing))}"
            )

    def _resolve_auth_headers(self, config: EndpointConfig) -> dict[str, str]:
        """Resolve auth headers from :class:`SecretProvider` if configured."""
        if not config.secret_key or self._secret_provider is None:
            return {}
        value = self._secret_provider.resolve(config.secret_key)
        if value is None:
            return {}

        auth_type = config.auth_type or AuthType.BEARER
        header_name = config.auth_header_name

        if auth_type == AuthType.BEARER:
            return {"Authorization": f"Bearer {value}"}
        if auth_type == AuthType.API_KEY:
            return {header_name: value}
        # CUSTOM_HEADER
        return {header_name: value}

    def _backoff_delay(self, attempt: int, config: EndpointConfig) -> float:
        """Compute jittered exponential backoff (D-05 formula)."""
        base = self._settings.retry_backoff_base
        delay = base * (2**attempt) + random.uniform(0, 0.1)  # noqa: S311
        return min(delay, self._settings.retry_max_delay)

    def _is_retryable_status(
        self,
        status_code: int,
        method: str,
        config: EndpointConfig,
    ) -> bool:
        """Decide whether *status_code* is retryable for *method*."""
        # If endpoint config explicitly sets retryable codes, honour them always.
        if config.retryable_status_codes is not None:
            return status_code in config.retryable_status_codes

        # For non-idempotent methods, do NOT retry on status codes by default.
        if method.upper() not in _IDEMPOTENT_METHODS:
            return False

        return status_code in self._settings.retryable_status_codes

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with full resilience pipeline.

        Pipeline order: rate limit -> capability check -> auth resolution
        -> circuit breaker -> retry loop with backoff -> audit record.
        """
        config = self._resolve_config(endpoint_config)
        endpoint_key = self._endpoint_key(url)

        # 1. Rate limiting
        limiter = self._get_rate_limiter(endpoint_key, config)
        if not await limiter.acquire():
            raise HttpRateLimitError(endpoint_key)

        # 2. Capability check
        self._check_capabilities(method, effective_capabilities)

        # 3. Auth headers
        auth_headers = self._resolve_auth_headers(config)
        if auth_headers:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.update(auth_headers)
            kwargs["headers"] = headers

        # 4. Circuit breaker
        breaker = self._breaker_registry.get(
            endpoint_key,
            failure_threshold=self._settings.circuit_breaker_threshold,
            reset_timeout=self._settings.circuit_breaker_reset_timeout,
        )
        await breaker.check()

        # 5. Retry loop
        max_retries = (
            config.max_retries if config.max_retries is not None else self._settings.max_retries
        )
        timeout_override = config.timeout
        if timeout_override is not None:
            kwargs["timeout"] = timeout_override

        last_error: str = ""
        retry_count = 0
        start = time.monotonic()

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)

                retryable = self._is_retryable_status(response.status_code, method, config)
                if retryable and attempt < max_retries:
                    last_error = f"HTTP {response.status_code}"
                    retry_count = attempt + 1
                    delay = self._backoff_delay(attempt, config)
                    await asyncio.sleep(delay)
                    continue

                if retryable:
                    # Last attempt still returned a retryable status — exhausted.
                    retry_count = attempt
                    last_error = f"HTTP {response.status_code}"
                    break

                # Success (or non-retryable status)
                await breaker.record_success()
                elapsed_ms = (time.monotonic() - start) * 1000
                self._call_records.append(
                    HttpCallRecord(
                        url=redact_url(url),
                        method=method.upper(),
                        status_code=response.status_code,
                        latency_ms=round(elapsed_ms, 2),
                        response_size_bytes=len(response.content) if response.content else None,
                        retry_count=retry_count,
                        circuit_breaker_state=breaker.state.value,
                    )
                )
                return response

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                await breaker.record_failure()
                last_error = f"{type(exc).__name__}: {exc}"
                retry_count = attempt + 1
                if attempt < max_retries:
                    delay = self._backoff_delay(attempt, config)
                    await asyncio.sleep(delay)

        # All retries exhausted
        elapsed_ms = (time.monotonic() - start) * 1000
        self._call_records.append(
            HttpCallRecord(
                url=redact_url(url),
                method=method.upper(),
                latency_ms=round(elapsed_ms, 2),
                retry_count=retry_count,
                error=last_error,
            )
        )
        raise HttpRetryExhaustedError(attempts=retry_count, last_error=last_error)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a GET request."""
        return await self.request(
            "GET",
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            **kwargs,
        )

    async def post(
        self,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a POST request."""
        return await self.request(
            "POST",
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            **kwargs,
        )

    async def put(
        self,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a PUT request."""
        return await self.request(
            "PUT",
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            **kwargs,
        )

    async def patch(
        self,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a PATCH request."""
        return await self.request(
            "PATCH",
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            **kwargs,
        )

    async def delete(
        self,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a DELETE request."""
        return await self.request(
            "DELETE",
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Audit / lifecycle
    # ------------------------------------------------------------------

    def drain_call_records(self) -> list[HttpCallRecord]:
        """Return accumulated call records and reset the internal list."""
        records = list(self._call_records)
        self._call_records.clear()
        return records

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient`."""
        await self._client.aclose()

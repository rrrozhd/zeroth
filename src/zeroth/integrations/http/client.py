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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from zeroth.integrations.http.circuit_breaker import CircuitBreakerRegistry, InMemoryTokenBucket
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

if TYPE_CHECKING:
    from zeroth.governance.policy.models import Capability
    from zeroth.platform.secrets.provider import SecretProvider

logger = logging.getLogger(__name__)

# HTTP methods considered idempotent (safe to retry on status codes).
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

#: Transport failures where the request provably never reached the peer.
#:
#: These are safe to replay whatever the method is: no side effect can have been
#: applied by a server that was never connected to. Treating them like a
#: mid-stream failure -- refusing to retry a POST -- withheld retry from the
#: single safest class there is, on a task about resilience.
_UNDELIVERED_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

#: Transport failures that are deterministic client-side faults.
#:
#: A malformed URL or an unsupported scheme cannot succeed on a second attempt.
#: Retrying them burned the backoff budget and charged the circuit breaker for a
#: host that was never dialled, and replacing them with HttpRetryExhaustedError
#: hid the only diagnosis that mattered.
_CLIENT_FAULT_ERRORS = (httpx.UnsupportedProtocol, httpx.LocalProtocolError)


@dataclass(frozen=True, slots=True)
class ObservedHttpResponse:
    """One response paired with its invocation-local sanitized call record."""

    response: httpx.Response
    call_record: HttpCallRecord


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
        from zeroth.governance.policy.models import Capability  # noqa: PLC0415

        read_caps = frozenset({Capability.NETWORK_READ, Capability.EXTERNAL_API_CALL})
        write_caps = frozenset({Capability.NETWORK_WRITE, Capability.EXTERNAL_API_CALL})
        required = read_caps if method.upper() in _IDEMPOTENT_METHODS else write_caps
        missing = required - effective_capabilities
        if missing:
            raise HttpClientError(
                f"Missing required capability for {method}: "
                f"{', '.join(sorted(str(c) for c in missing))}"
            )

    async def _resolve_auth_headers(self, config: EndpointConfig) -> dict[str, str]:
        """Resolve auth headers from :class:`SecretProvider` if configured.

        Async so a Vault-backed provider's HTTP fetch on a cache miss runs off
        the event loop instead of stalling every in-flight request.
        """
        if not config.secret_key or self._secret_provider is None:
            return {}
        from zeroth.platform.secrets.provider import resolve_async  # noqa: PLC0415

        value = await resolve_async(self._secret_provider, config.secret_key)
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

    def _method_may_retry(self, method: str, config: EndpointConfig) -> bool:
        """Decide whether *method* may be redelivered on this endpoint at all.

        One rule serves both retry paths — status and transport exception — so a
        POST cannot be replayed down one path while being refused on the other.
        An endpoint that declares its own ``retryable_status_codes`` has had its
        retry policy set deliberately and keeps the existing escape hatch.
        """
        if config.retryable_status_codes:
            return True
        return method.upper() in _IDEMPOTENT_METHODS

    def _is_unhealthy_status(self, status_code: int, config: EndpointConfig) -> bool:
        """Whether *status_code* means the endpoint answered but failed.

        Deliberately independent of the request method. Whether a call may be
        replayed is a property of the method; whether the endpoint is failing is
        a property of the response, and the circuit breaker cares only about the
        second.
        """
        codes = (
            config.retryable_status_codes
            if config.retryable_status_codes is not None
            else self._settings.retryable_status_codes
        )
        return status_code in codes

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
        if not self._method_may_retry(method, config):
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
        return await self._request(
            method,
            url,
            endpoint_config=endpoint_config,
            effective_capabilities=effective_capabilities,
            record_sink=self._call_records.append,
            **kwargs,
        )

    async def request_with_record(
        self,
        method: str,
        url: str,
        *,
        endpoint_config: EndpointConfig | None = None,
        effective_capabilities: set[Capability] | None = None,
        **kwargs: Any,
    ) -> ObservedHttpResponse:
        """Execute one request and return only that request's call record.

        Unlike :meth:`drain_call_records`, this API has no process-global drain
        race: concurrent workflow nodes each own a local record sink. Expected
        resilient-client failures carry the same record on
        ``error.http_call_record`` so the signed failure audit is equally
        attributable.
        """
        records: list[HttpCallRecord] = []
        started = time.monotonic()
        try:
            response = await self._request(
                method,
                url,
                endpoint_config=endpoint_config,
                effective_capabilities=effective_capabilities,
                record_sink=records.append,
                **kwargs,
            )
        except Exception as error:
            record = records[-1] if records else HttpCallRecord(
                url=redact_url(url),
                method=method.upper(),
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                error=type(error).__name__,
            )
            error.http_call_record = record  # type: ignore[attr-defined]
            raise
        if len(records) != 1:
            raise RuntimeError("resilient HTTP request produced no invocation record")
        return ObservedHttpResponse(response=response, call_record=records[0])

    async def _request(
        self,
        method: str,
        url: str,
        *,
        endpoint_config: EndpointConfig | None,
        effective_capabilities: set[Capability] | None,
        record_sink: Callable[[HttpCallRecord], None],
        **kwargs: Any,
    ) -> httpx.Response:
        """Internal request pipeline parameterized by an invocation record sink."""
        config = self._resolve_config(endpoint_config)
        endpoint_key = self._endpoint_key(url)

        # 1. Rate limiting
        limiter = self._get_rate_limiter(endpoint_key, config)
        if not await limiter.acquire():
            raise HttpRateLimitError(endpoint_key)

        # 2. Capability check
        self._check_capabilities(method, effective_capabilities)

        # 3. Auth headers
        auth_headers = await self._resolve_auth_headers(config)
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
        try:
            await breaker.check()
        except CircuitOpenError:
            record_sink(
                HttpCallRecord(
                    url=redact_url(url),
                    method=method.upper(),
                    latency_ms=0.0,
                    retry_count=0,
                    circuit_breaker_state="open",
                    error="circuit_open",
                )
            )
            raise

        # 5. Retry loop
        timeout_override = config.timeout
        if timeout_override is not None:
            kwargs["timeout"] = timeout_override

        return await self._deliver(
            method,
            url,
            config=config,
            breaker=breaker,
            record_sink=record_sink,
            **kwargs,
        )

    async def _deliver(
        self,
        method: str,
        url: str,
        *,
        config: EndpointConfig,
        breaker: Any,
        record_sink: Callable[[HttpCallRecord], None],
        **kwargs: Any,
    ) -> httpx.Response:
        """Run the retry loop for one already-admitted request.

        Split out of :meth:`request` so the admission pipeline (rate limit,
        capabilities, auth, breaker check) and the delivery loop each stay
        readable on their own.
        """
        max_retries = (
            config.max_retries if config.max_retries is not None else self._settings.max_retries
        )
        may_retry = self._method_may_retry(method, config)

        last_error: str = ""
        retry_count = 0
        start = time.monotonic()

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            # Every transport-layer failure, not only timeout and connect:
            # a half-closed peer (RemoteProtocolError) or a mid-stream
            # read/write error is the same kind of endpoint failure, and used to
            # escape raw with no retry, no breaker accounting and no audit record.
            except _CLIENT_FAULT_ERRORS:
                # Deterministic and local: no retry, no breaker charge against a
                # host that was never dialled, and the original exception is
                # raised rather than being flattened into a retry-exhaustion.
                raise
            except httpx.TransportError as exc:
                await breaker.record_failure()
                last_error = f"{type(exc).__name__}: {exc}"
                retry_count = attempt + 1
                # Redelivering a non-idempotent request can duplicate a side
                # effect the peer may already have applied -- but only if it
                # could have been applied at all. A connect or pool failure
                # never reached the peer, so it is replayable regardless.
                undelivered = isinstance(exc, _UNDELIVERED_ERRORS)
                if not (may_retry or undelivered):
                    break
                if attempt < max_retries:
                    await asyncio.sleep(self._backoff_delay(attempt, config))
                continue

            # Two independent questions, previously conflated. "Is this endpoint
            # healthy?" is a property of the response alone. "May this request be
            # replayed?" depends on the method. Answering the first with the
            # second meant a 503 answered to a POST recorded a breaker SUCCESS
            # and zeroed the counter a concurrent GET had just raised, so on any
            # endpoint carrying mixed traffic the breaker never opened at all.
            unhealthy = self._is_unhealthy_status(response.status_code, config)
            if unhealthy:
                await breaker.record_failure()
            else:
                await breaker.record_success()

            if not self._is_retryable_status(response.status_code, method, config):
                self._record_success(
                    url,
                    method,
                    response,
                    retry_count,
                    breaker,
                    start,
                    record_sink,
                )
                return response
            last_error = f"HTTP {response.status_code}"
            if attempt >= max_retries:
                retry_count = attempt
                break
            retry_count = attempt + 1
            await asyncio.sleep(self._backoff_delay(attempt, config))

        record_sink(
            HttpCallRecord(
                url=redact_url(url),
                method=method.upper(),
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                retry_count=retry_count,
                error=last_error,
            )
        )
        raise HttpRetryExhaustedError(attempts=retry_count, last_error=last_error)

    def _record_success(
        self,
        url: str,
        method: str,
        response: httpx.Response,
        retry_count: int,
        breaker: Any,
        start: float,
        record_sink: Callable[[HttpCallRecord], None],
    ) -> None:
        """Append the audit record for a delivered response."""
        record_sink(
            HttpCallRecord(
                url=redact_url(url),
                method=method.upper(),
                status_code=response.status_code,
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                response_size_bytes=len(response.content) if response.content else None,
                retry_count=retry_count,
                circuit_breaker_state=breaker.state.value,
            )
        )

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

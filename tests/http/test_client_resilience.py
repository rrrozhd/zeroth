"""Resilience behaviour of :class:`ResilientHttpClient` (ZER-48 / G05a).

The suite that existed before this one drove the circuit breaker with
``ConnectError`` only, which is why three retry-path defects survived it:

* a transport exception was retried for **every** method, including
  non-idempotent ones, while the idempotency guard sat unused a few lines above;
* a retryable **status** never recorded a breaker failure, so an endpoint that
  was up but answering 5xx could never trip the breaker;
* only ``TimeoutException`` and ``ConnectError`` were caught, so every other
  transport error escaped raw with no retry, no breaker accounting and no
  ``HttpCallRecord``.

Each test here drives one of those paths end to end through a mock transport.
"""

from __future__ import annotations

import httpx
import pytest

from zeroth.integrations.http.circuit_breaker import CircuitState
from zeroth.integrations.http.client import ResilientHttpClient
from zeroth.integrations.http.errors import HttpRetryExhaustedError
from zeroth.integrations.http.models import EndpointConfig
from zeroth.platform.config.models import HttpClientSettings

_URL = "https://api.example.com/resource"


def _fast_settings(**overrides: object) -> HttpClientSettings:
    """Settings with backoff removed so retries do not slow the suite."""
    base: dict[str, object] = {"retry_backoff_base": 0.0, "retry_max_delay": 0.0}
    base.update(overrides)
    return HttpClientSettings(**base)  # type: ignore[arg-type]


def _client(settings: HttpClientSettings, handler) -> ResilientHttpClient:  # noqa: ANN001
    client = ResilientHttpClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _recording_handler(response_factory):  # noqa: ANN001, ANN202
    """Return ``(handler, methods)`` where *methods* logs every delivered method."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return response_factory(request)

    return handler, methods


def _raising_handler(exc_factory):  # noqa: ANN001, ANN202
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        raise exc_factory(request)

    return handler, methods


class TestIdempotencyGatedRetry:
    """A transport exception must obey the same idempotency rule as a status."""

    @pytest.mark.asyncio
    async def test_post_timeout_is_not_retried(self) -> None:
        """AC3 — a POST that times out is delivered exactly once."""
        handler, methods = _raising_handler(lambda r: httpx.ReadTimeout("slow", request=r))
        client = _client(_fast_settings(), handler)

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(_URL, json={"charge": 1})

        assert methods == ["POST"], f"POST was delivered {len(methods)} times: {methods}"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_timeout_is_still_retried(self) -> None:
        """The gate must not over-correct — idempotent methods keep retrying."""
        handler, methods = _raising_handler(lambda r: httpx.ReadTimeout("slow", request=r))
        client = _client(_fast_settings(max_retries=2), handler)

        with pytest.raises(HttpRetryExhaustedError):
            await client.get(_URL)

        assert methods == ["GET", "GET", "GET"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_explicit_endpoint_policy_still_retries_a_post(self) -> None:
        """An endpoint that declares its own retry policy keeps the escape hatch."""
        handler, methods = _raising_handler(lambda r: httpx.ReadTimeout("slow", request=r))
        client = _client(_fast_settings(max_retries=1), handler)
        config = EndpointConfig(retryable_status_codes={503})

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(_URL, endpoint_config=config, json={"charge": 1})

        assert methods == ["POST", "POST"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unretried_post_is_still_audited(self) -> None:
        """Refusing to retry must not also drop the audit record."""
        handler, _ = _raising_handler(lambda r: httpx.ReadTimeout("slow", request=r))
        client = _client(_fast_settings(), handler)

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(_URL, json={"charge": 1})

        records = client.drain_call_records()
        assert len(records) == 1
        assert records[0].method == "POST"
        assert records[0].error and "ReadTimeout" in records[0].error
        await client.aclose()


class TestBreakerOnRetryableStatus:
    """A 5xx is an endpoint failure and must be counted as one."""

    @pytest.mark.asyncio
    async def test_opens_on_repeated_5xx(self) -> None:
        """AC2 — an endpoint that is up but answering 503 trips the breaker."""
        settings = _fast_settings(max_retries=0, circuit_breaker_threshold=3)
        handler, _ = _recording_handler(lambda r: httpx.Response(503))
        client = _client(settings, handler)

        for _ in range(3):
            with pytest.raises(HttpRetryExhaustedError):
                await client.get(_URL)

        breaker = client._breaker_registry.get(
            "api.example.com:443",
            failure_threshold=settings.circuit_breaker_threshold,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        assert breaker.state is CircuitState.OPEN
        await client.aclose()

    @pytest.mark.asyncio
    async def test_success_after_5xx_resets_the_count(self) -> None:
        """Counting a 5xx must not make the breaker latch on a recovered endpoint."""
        settings = _fast_settings(max_retries=0, circuit_breaker_threshold=3)
        codes = iter([503, 503, 200, 503, 503])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(next(codes))

        client = _client(settings, handler)

        for _ in range(2):
            with pytest.raises(HttpRetryExhaustedError):
                await client.get(_URL)
        assert (await client.get(_URL)).status_code == 200
        for _ in range(2):
            with pytest.raises(HttpRetryExhaustedError):
                await client.get(_URL)

        breaker = client._breaker_registry.get(
            "api.example.com:443",
            failure_threshold=settings.circuit_breaker_threshold,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        assert breaker.state is CircuitState.CLOSED
        await client.aclose()


class TestWiderTransportErrors:
    """Every transport error, not just timeout and connect, is handled."""

    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(
                lambda r: httpx.RemoteProtocolError("peer hung up", request=r),
                id="remote_protocol_error",
            ),
            pytest.param(lambda r: httpx.ReadError("read failed"), id="read_error"),
            pytest.param(lambda r: httpx.WriteError("write failed"), id="write_error"),
        ],
    )
    @pytest.mark.asyncio
    async def test_transport_error_is_retried_counted_and_recorded(
        self,
        exc_factory,  # noqa: ANN001
    ) -> None:
        settings = _fast_settings(max_retries=2, circuit_breaker_threshold=99)
        handler, methods = _raising_handler(exc_factory)
        client = _client(settings, handler)

        with pytest.raises(HttpRetryExhaustedError):
            await client.get(_URL)

        assert methods == ["GET", "GET", "GET"], "transport error was not retried"

        records = client.drain_call_records()
        assert len(records) == 1, "transport failure left no audit record"
        assert records[0].error

        breaker = client._breaker_registry.get(
            "api.example.com:443",
            failure_threshold=settings.circuit_breaker_threshold,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        assert breaker._failure_count > 0, "transport failure was not counted by the breaker"
        await client.aclose()

"""Endpoint health and replay eligibility are separate questions (ZER-48 audit).

A07-4's original fix recorded a breaker failure for a *retryable* status. That
conflated two independent things: whether the endpoint is failing, which is a
property of the response, and whether this request may be replayed, which is a
property of the method.

The consequence was measured during the initial audit: a 503 answered to a POST
is not retryable, so it took the `record_success()` branch and zeroed the failure
count a concurrent GET had just raised. Alternating GET and POST 503s against one
endpoint oscillated 4 → 0 → 4 → 0 and the breaker never opened, so the fix worked
only on read-only endpoints — which is not what it claimed.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from zeroth.integrations.http.circuit_breaker import CircuitState
from zeroth.integrations.http.client import ResilientHttpClient
from zeroth.integrations.http.errors import CircuitOpenError, HttpRetryExhaustedError
from zeroth.integrations.http.models import EndpointConfig
from zeroth.platform.config.models import HttpClientSettings

_URL = "https://api.example.com/resource"
_KEY = "api.example.com:443"


def _client(**overrides: object) -> ResilientHttpClient:
    base: dict[str, object] = {
        "retry_backoff_base": 0.0,
        "retry_max_delay": 0.0,
        "max_retries": 0,
        "circuit_breaker_threshold": 3,
    }
    base.update(overrides)
    settings = HttpClientSettings(**base)  # type: ignore[arg-type]
    client = ResilientHttpClient(settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    return client


def _breaker(client: ResilientHttpClient):  # noqa: ANN202
    return client._breaker_registry.get(_KEY, failure_threshold=3, reset_timeout=30.0)


async def _attempt(client: ResilientHttpClient, method: str) -> None:
    """Issue one request, tolerating both failure modes.

    Once the breaker opens the next call is refused up front with
    ``CircuitOpenError`` instead of reaching the transport -- which is the point
    of the breaker, so a helper that only caught the exhaustion error would fail
    on success.
    """
    with contextlib.suppress(HttpRetryExhaustedError, CircuitOpenError):
        await client.request(method, _URL)


class TestMixedTrafficStillTripsTheBreaker:
    @pytest.mark.asyncio
    async def test_alternating_get_and_post_5xx_opens_the_breaker(self) -> None:
        """The measured defect: the POST used to zero what the GET had raised."""
        client = _client()

        for method in ("GET", "POST", "GET", "POST", "GET", "POST"):
            await _attempt(client, method)

        assert _breaker(client).state is CircuitState.OPEN
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_only_5xx_opens_the_breaker(self) -> None:
        """A write-only endpoint that is failing is still a failing endpoint."""
        client = _client()

        for _ in range(3):
            await _attempt(client, "POST")

        assert _breaker(client).state is CircuitState.OPEN
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_non_retryable_post_5xx_is_not_recorded_as_a_success(self) -> None:
        """The precise inversion: a hard 503 must never raise the success count."""
        client = _client()
        breaker = _breaker(client)
        await breaker.record_failure()
        before = breaker._failure_count

        await _attempt(client, "POST")

        assert breaker._failure_count > before, "a 503 lowered or held the failure count"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_healthy_response_still_resets_the_breaker(self) -> None:
        """Separating the two questions must not stop recovery from being noticed."""
        client = _client()
        breaker = _breaker(client)
        await _attempt(client, "GET")
        assert breaker._failure_count > 0

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
        assert (await client.request("GET", _URL)).status_code == 200

        assert breaker._failure_count == 0
        assert breaker.state is CircuitState.CLOSED
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_non_retryable_4xx_is_not_an_endpoint_failure(self) -> None:
        """404 is the caller's problem, not the endpoint's; it must not trip anything."""
        client = _client()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        )

        for _ in range(5):
            assert (await client.request("GET", _URL)).status_code == 404

        assert _breaker(client).state is CircuitState.CLOSED
        await client.aclose()


class TestExplicitStatusPolicyDoesNotUnlockReplay:
    @pytest.mark.asyncio
    async def test_an_empty_status_set_does_not_permit_post_replay(self) -> None:
        """The most restrictive policy must not grant the most permissive replay.

        `retryable_status_codes=set()` says "retry no status at all". It used to
        satisfy the `is not None` escape hatch and let a POST be redelivered four
        times after a transport error.

        Driven with `ReadError` rather than `ConnectError` on purpose: a connect
        failure never reached the peer and is now replayable whatever the method
        is, so it would not exercise the method gate this test is about.
        """
        delivered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            delivered.append(request.method)
            raise httpx.ReadError("mid-stream")

        settings = HttpClientSettings(
            retry_backoff_base=0.0, retry_max_delay=0.0, max_retries=3
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(
                _URL, endpoint_config=EndpointConfig(retryable_status_codes=set()), json={}
            )

        assert delivered == ["POST"], f"POST replayed {len(delivered)} times"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_populated_status_set_keeps_the_escape_hatch(self) -> None:
        """An endpoint that declares a real retry policy keeps its opt-in."""
        delivered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            delivered.append(request.method)
            raise httpx.ReadError("mid-stream")

        settings = HttpClientSettings(
            retry_backoff_base=0.0, retry_max_delay=0.0, max_retries=1
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(
                _URL, endpoint_config=EndpointConfig(retryable_status_codes={503}), json={}
            )

        assert delivered == ["POST", "POST"]
        await client.aclose()


class TestUndeliveredErrorsAreReplayableRegardlessOfMethod:
    """A request that never reached the peer cannot have applied a side effect."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(lambda r: httpx.ConnectError("down", request=r), id="connect_error"),
            pytest.param(lambda r: httpx.ConnectTimeout("slow", request=r), id="connect_timeout"),
            pytest.param(lambda r: httpx.PoolTimeout("full"), id="pool_timeout"),
        ],
    )
    async def test_a_post_is_retried_when_the_peer_was_never_reached(
        self,
        exc_factory,  # noqa: ANN001
    ) -> None:
        delivered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            delivered.append(request.method)
            raise exc_factory(request)

        settings = HttpClientSettings(
            retry_backoff_base=0.0, retry_max_delay=0.0, max_retries=2
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(_URL, json={})

        assert delivered == ["POST", "POST", "POST"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_post_is_not_retried_when_the_peer_may_have_applied_it(self) -> None:
        """The mid-stream case keeps the idempotency gate."""
        delivered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            delivered.append(request.method)
            raise httpx.RemoteProtocolError("peer hung up", request=request)

        settings = HttpClientSettings(
            retry_backoff_base=0.0, retry_max_delay=0.0, max_retries=2
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with pytest.raises(HttpRetryExhaustedError):
            await client.post(_URL, json={})

        assert delivered == ["POST"]
        await client.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(lambda r: httpx.UnsupportedProtocol("bad scheme"), id="unsupported"),
            pytest.param(lambda r: httpx.LocalProtocolError("bad request"), id="local_protocol"),
        ],
    )
    async def test_a_deterministic_client_fault_is_raised_not_retried(
        self,
        exc_factory,  # noqa: ANN001
    ) -> None:
        """These cannot succeed on a second attempt, and the real type matters."""
        delivered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            delivered.append(request.method)
            raise exc_factory(request)

        settings = HttpClientSettings(
            retry_backoff_base=0.0, retry_max_delay=0.0, max_retries=3
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with pytest.raises(httpx.TransportError) as excinfo:
            await client.get(_URL)

        assert not isinstance(excinfo.value, HttpRetryExhaustedError)
        assert delivered == ["GET"]
        assert _breaker(client)._failure_count == 0, "charged for a host never dialled"
        await client.aclose()

"""Outbound clients are shared and bounded, not rebuilt per request.

``cost_api``, ``regulus_proxy_api`` and ``health`` each built a throwaway
``httpx.AsyncClient`` inside the request handler, so every call paid a fresh TCP
and TLS handshake and no connection was ever kept alive (A02-16).

On ``/health/ready`` the same shape is worse than slow. That route answers
*before* authentication, and its Redis client came from ``redis_from_url`` with
neither ``socket_timeout`` nor ``socket_connect_timeout`` set -- redis-py leaves
both ``None`` -- so an unauthenticated caller could drive unbounded, unbounded-
duration client creation (A02-5).

Note what is deliberately *not* asserted here: rate limiting. The
re-verification of A02-5 separately measured that no rate limiting exists
anywhere on the HTTP boundary, but that is a different control class from "one
governed client layer" and is recorded as a deferred observation rather than
folded into this ticket.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from zeroth.integrations.http.factory import (
    DEFAULT_LIMITS,
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    aclose_all,
    governed_async_client,
    reset_process_cache,
)


class _App:
    """Minimal stand-in for the ``app`` the factory anchors its cache on."""

    def __init__(self) -> None:
        self.state = type("State", (), {})()


@pytest.fixture(autouse=True)
def _clean_process_cache():  # noqa: ANN202
    reset_process_cache()
    yield
    reset_process_cache()


class TestClientIsShared:
    @pytest.mark.asyncio
    async def test_two_calls_reuse_one_client(self) -> None:
        app = _App()

        first = await governed_async_client(purpose="p", timeout=2.0, app=app)
        second = await governed_async_client(purpose="p", timeout=2.0, app=app)

        assert first is second
        await aclose_all(app)

    @pytest.mark.asyncio
    async def test_concurrent_first_callers_get_the_same_client(self) -> None:
        """A readiness probe is exactly the endpoint that is hit concurrently."""
        app = _App()

        clients = await asyncio.gather(
            *(governed_async_client(purpose="p", timeout=2.0, app=app) for _ in range(20))
        )

        assert len({id(c) for c in clients}) == 1, "a race built more than one client"
        await aclose_all(app)

    @pytest.mark.asyncio
    async def test_distinct_purposes_do_not_share(self) -> None:
        """One caller's pool exhaustion must not starve another's."""
        app = _App()

        a = await governed_async_client(purpose="a", timeout=2.0, app=app)
        b = await governed_async_client(purpose="b", timeout=2.0, app=app)

        assert a is not b
        await aclose_all(app)

    @pytest.mark.asyncio
    async def test_separate_apps_do_not_share(self) -> None:
        """A test building a fresh app must not inherit a closed loop's client."""
        first_app, second_app = _App(), _App()

        a = await governed_async_client(purpose="p", timeout=2.0, app=first_app)
        b = await governed_async_client(purpose="p", timeout=2.0, app=second_app)

        assert a is not b
        await aclose_all(first_app)
        await aclose_all(second_app)


class TestClientIsBounded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, 0, 0.0, -1.0, float("inf"), float("nan")])
    async def test_a_timeout_that_is_not_a_real_bound_is_refused(self, bad: object) -> None:
        with pytest.raises(ValueError, match="timeout"):
            await governed_async_client(purpose="p", timeout=bad, app=_App())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_client_carries_the_timeout_and_a_pool_ceiling(self) -> None:
        app = _App()

        client = await governed_async_client(purpose="p", timeout=3.5, app=app)

        assert client.timeout.connect == 3.5
        assert DEFAULT_LIMITS.max_connections is not None
        await aclose_all(app)

    def test_the_redis_probe_bounds_are_positive(self) -> None:
        """redis-py defaults both of these to ``None``; the probe must not."""
        assert REDIS_SOCKET_TIMEOUT_SECONDS > 0
        assert REDIS_CONNECT_TIMEOUT_SECONDS > 0


class TestShutdown:
    @pytest.mark.asyncio
    async def test_aclose_all_is_idempotent(self) -> None:
        app = _App()
        await governed_async_client(purpose="p", timeout=2.0, app=app)

        await aclose_all(app)
        await aclose_all(app)

    @pytest.mark.asyncio
    async def test_a_closed_cache_builds_a_fresh_client(self) -> None:
        """Shutdown must not leave the cache handing out closed clients."""
        app = _App()
        first = await governed_async_client(purpose="p", timeout=2.0, app=app)
        await aclose_all(app)

        second = await governed_async_client(purpose="p", timeout=2.0, app=app)

        assert second is not first
        assert not second.is_closed
        await aclose_all(app)


class TestShutdownIsWired:
    """A cache of long-lived clients that nothing closes is a leak, not a pool."""

    @pytest.mark.asyncio
    async def test_the_lifespan_closes_the_governed_clients(self) -> None:
        from zeroth.service.bootstrap.lifecycle import _close_governed_clients

        app = _App()
        client = await governed_async_client(purpose="p", timeout=2.0, app=app)
        assert not client.is_closed

        await _close_governed_clients(app)  # type: ignore[arg-type]

        assert client.is_closed

    @pytest.mark.asyncio
    async def test_a_failure_while_closing_does_not_escape_teardown(self) -> None:
        """Teardown is already reporting something; this must not mask it."""
        from zeroth.service.bootstrap.lifecycle import _close_governed_clients

        class _Exploding:
            async def aclose(self) -> None:
                raise RuntimeError("close failed")

        app = _App()
        cache = __import__(
            "zeroth.integrations.http.factory", fromlist=["client_cache"]
        ).client_cache(app)
        await cache.get_or_create(("boom",), lambda: _completed(_Exploding()))

        await _close_governed_clients(app)  # type: ignore[arg-type]  # must not raise


async def _completed(value: object) -> object:
    """Return *value* from an awaitable, for cache builders under test."""
    return value


class TestCallSitesUseTheFactory:
    """The point of the factory is that the call sites actually reach it."""

    @pytest.mark.asyncio
    async def test_the_regulus_cost_query_reuses_one_client(self) -> None:
        from zeroth.service.api.cost_api import _regulus_client

        app = _App()
        seen: list[httpx.AsyncClient] = []

        class _Request:
            def __init__(self) -> None:
                self.app = app

        for _ in range(3):
            seen.append(await _regulus_client(_Request(), 2.0))  # type: ignore[arg-type]

        assert len({id(c) for c in seen}) == 1
        await aclose_all(app)

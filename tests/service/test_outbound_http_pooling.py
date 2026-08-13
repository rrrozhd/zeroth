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
import logging

import httpx
import pytest

from zeroth.integrations.http.factory import (
    DEFAULT_LIMITS,
    REDIS_CONNECT_TIMEOUT_SECONDS,
    REDIS_SOCKET_TIMEOUT_SECONDS,
    aclose_all,
    client_cache,
    governed_async_client,
    governed_redis_client,
    reset_process_cache,
)

#: The logger ``aclose_all`` reports a failed client close on.
_FACTORY_LOGGER = "zeroth.integrations.http.factory"


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


class _RaisingClient:
    """A cached client whose ``aclose`` always fails."""

    async def aclose(self) -> None:
        """Fail the way a client holding a wedged connection would."""
        raise RuntimeError("aclose failed")


class _RecordingClient:
    """A cached client that records whether shutdown ever reached it."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        """Record that this client was closed."""
        self.closed = True


class TestShutdownSurvivesAFailingClient:
    """One client that cannot close must not strand every client behind it."""

    @pytest.mark.asyncio
    async def test_a_raising_aclose_does_not_strand_the_clients_behind_it(self) -> None:
        """Close the rest of the cache after one client fails to close.

        ``aclose_all`` empties the cache under the lock and closes what it took
        afterwards, serially. Nothing holds a reference to the remainder, so a
        raiser that aborted that loop leaked every client behind it for the life
        of the process. The cache is heterogeneous -- httpx clients and a redis
        client -- so they do not even fail the same way.
        """
        app = _App()
        cache = client_cache(app)
        good = _RecordingClient()
        await cache.get_or_create(("raiser",), lambda: _completed(_RaisingClient()))
        await cache.get_or_create(("good",), lambda: _completed(good))

        await aclose_all(app)

        assert good.closed, "a failing aclose() stranded the client queued behind it"

    @pytest.mark.asyncio
    async def test_a_failing_close_is_logged_rather_than_passed_over(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Leave a trace when a client cannot be closed; do not swallow it."""
        app = _App()
        cache = client_cache(app)
        await cache.get_or_create(("raiser",), lambda: _completed(_RaisingClient()))

        with caplog.at_level(logging.WARNING, logger=_FACTORY_LOGGER):
            await aclose_all(app)

        assert [r for r in caplog.records if r.name == _FACTORY_LOGGER], (
            "a client that failed to close left no record at all"
        )


class TestRedisProbeBoundsReachRedis:
    """A constant nobody passes to redis-py is documentation, not a bound."""

    @pytest.mark.asyncio
    async def test_the_redis_client_is_built_with_both_socket_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert the kwargs that reach ``from_url``, not just the constants.

        Both constants can be correct while neither is handed to redis-py: an
        audit dropped both kwargs from the builder and the whole suite stayed
        green, because the only assertion on them read the constants directly.
        """
        recorded: dict[str, object] = {}

        def _fake_from_url(url: str, **kwargs: object) -> _RecordingClient:
            recorded["url"] = url
            recorded.update(kwargs)
            return _RecordingClient()

        monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)
        app = _App()

        await governed_redis_client("redis://localhost:6379/0", purpose="ready", app=app)

        assert recorded.get("socket_timeout") == REDIS_SOCKET_TIMEOUT_SECONDS, (
            f"the read bound never reached redis-py: {recorded}"
        )
        assert recorded.get("socket_connect_timeout") == REDIS_CONNECT_TIMEOUT_SECONDS, (
            f"the connect bound never reached redis-py: {recorded}"
        )
        await aclose_all(app)

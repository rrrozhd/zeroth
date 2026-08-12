"""Governed factory for the outbound clients used off the agent call path.

:class:`~zeroth.integrations.http.client.ResilientHttpClient` is the governed
HTTP layer for agent traffic: it already owns one pooled
:class:`httpx.AsyncClient` and layers retry, circuit breaking, rate limiting and
audit on top of it. Nothing here duplicates any of that.

What this module adds is the *other* half of the same discipline, for the call
sites that are not agent traffic -- readiness probes, the cost queries, the
Regulus console proxy. Those built a throwaway ``httpx.AsyncClient`` inside each
request handler (A02-16), so every call paid a fresh TCP and TLS handshake and
no connection was ever kept alive. On ``/health/ready``, which answers *before*
authentication, that also meant an unauthenticated caller could drive unbounded
client and connection creation (A02-5).

The contract this module enforces:

* one client per ``(purpose, base_url, timeout, redirects, transport)`` key,
  created lazily on first use and reused by every later caller;
* a **mandatory** positive, finite timeout -- ``None``, zero, negative, ``nan``
  and ``inf`` are all refused loudly rather than silently meaning "no bound";
* explicit :class:`httpx.Limits`, so the pool has a ceiling;
* an explicit :func:`aclose_all` for shutdown, safe to call more than once.

Cache placement is deliberate. When a FastAPI app is available the cache is
anchored on ``app.state``, so it lives and dies with the app rather than with
the process: a test that builds a fresh app per case cannot inherit a client
bound to an event loop that has already closed. Callers with no app fall back to
a process-wide cache, which :func:`reset_process_cache` clears.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: Default outbound timeout, in seconds, for a governed non-agent client.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Ceiling on simultaneously open connections held by one governed client.
MAX_CONNECTIONS = 100

#: How many of those connections may be parked idle for keep-alive reuse.
MAX_KEEPALIVE_CONNECTIONS = 20

#: How long, in seconds, an idle keep-alive connection survives before eviction.
KEEPALIVE_EXPIRY_SECONDS = 5.0

#: The single pool bound every governed non-agent client is built with.
DEFAULT_LIMITS = httpx.Limits(
    max_connections=MAX_CONNECTIONS,
    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
)

#: Attribute on ``app.state`` under which an app's client cache is anchored.
CACHE_STATE_ATTRIBUTE = "governed_http_clients"

_PROCESS_CACHES: dict[str, GovernedClientCache] = {}
_PROCESS_CACHE_KEY = "default"


def validate_timeout(timeout: float | None, *, name: str = "timeout") -> float:
    """Return *timeout* as a positive number of seconds, or refuse it loudly.

    Every spelling of "no bound" is rejected here rather than passed through to
    ``httpx``, where ``None`` means *wait forever*. An unbounded outbound call
    from an unauthenticated endpoint is the failure this whole module exists to
    prevent, so it must not be reachable by omission.

    Args:
        timeout: The candidate timeout in seconds.
        name: The parameter name to quote in the error message.

    Returns:
        The timeout as a ``float``.

    Raises:
        ValueError: If *timeout* is ``None``, not a real number, or not a
            positive finite value.

    """
    if timeout is None:
        raise ValueError(f"{name} must be a positive number of seconds, not None")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError(f"{name} must be a positive number of seconds, got {timeout!r}")
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {seconds!r}")
    return seconds


class GovernedClientCache:
    """One long-lived client per key, built once and handed to every caller.

    The lock is load-bearing, not decorative: a readiness probe is precisely the
    endpoint that is hit concurrently, and the builder is awaited *inside* the
    critical section, so two first-callers racing on a cold key cannot each
    construct a client and leave one of them orphaned and unclosed.
    """

    def __init__(self) -> None:
        self._clients: dict[Any, Any] = {}
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        """Return how many clients this cache currently holds open."""
        return len(self._clients)

    async def get_or_create(self, key: Any, build: Callable[[], Awaitable[Any]]) -> Any:
        """Return the client cached under *key*, building it on first use.

        Args:
            key: Any hashable identity for the client. Everything that changes
                how the client is constructed belongs in it, or a caller can be
                handed a client configured for someone else.
            build: Awaitable factory called at most once per key, under the
                lock. It is awaited rather than called so that the fast path and
                the contended path go through the same suspension point.

        Returns:
            The cached client, which may have been created by another caller.

        """
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            # Re-check: a caller that was waiting on the lock may have built it.
            cached = self._clients.get(key)
            if cached is not None:
                return cached
            client = await build()
            self._clients[key] = client
            return client

    async def aclose_all(self) -> None:
        """Close every client held, and forget them. Safe to call repeatedly.

        The cache is emptied *before* anything is closed, so a second call --
        or a concurrent one -- finds nothing left to close rather than closing
        the same client twice or handing out a client mid-teardown.
        """
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()


def client_cache(app: Any | None = None) -> GovernedClientCache:
    """Return the client cache anchored on *app*, or the process-wide one.

    Args:
        app: Anything exposing a ``state`` namespace -- in practice the FastAPI
            app reached through ``request.app``. ``None``, or an object with no
            ``state``, selects the process-wide fallback.

    Returns:
        The cache to use. Creation is a plain attribute write with no await in
        between, so it is atomic with respect to other coroutines on the loop.

    """
    state = getattr(app, "state", None)
    if state is None:
        cache = _PROCESS_CACHES.get(_PROCESS_CACHE_KEY)
        if cache is None:
            cache = GovernedClientCache()
            _PROCESS_CACHES[_PROCESS_CACHE_KEY] = cache
        return cache

    cache = getattr(state, CACHE_STATE_ATTRIBUTE, None)
    if not isinstance(cache, GovernedClientCache):
        cache = GovernedClientCache()
        setattr(state, CACHE_STATE_ATTRIBUTE, cache)
    return cache


async def governed_async_client(
    *,
    purpose: str,
    timeout: float | None,
    base_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    app: Any | None = None,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """Return the shared, bounded :class:`httpx.AsyncClient` for one purpose.

    Args:
        purpose: Short stable name for the caller, e.g. ``"regulus-cost"``. Two
            purposes never share a client even against the same host, so one
            caller's pool exhaustion cannot starve another's.
        timeout: Mandatory positive timeout in seconds. See
            :func:`validate_timeout`.
        base_url: The upstream this client will dial. Part of the key.
        transport: Optional transport override. Part of the key, so swapping it
            yields a different client instead of silently reusing the old one.
        app: Cache anchor; see :func:`client_cache`.
        follow_redirects: Defaults to ``False``. Part of the key.

    Returns:
        A client shared with every other caller that asked for the same key.

    Raises:
        ValueError: If *timeout* is not a positive finite number of seconds.

    """
    seconds = validate_timeout(timeout)
    key = ("http", purpose, str(base_url or ""), seconds, follow_redirects, transport)

    async def build() -> httpx.AsyncClient:
        # base_url is part of the key, so it must also reach the client -- a key
        # that distinguishes clients by a value the client does not carry hands
        # back something configured for a different upstream.
        extra: dict[str, Any] = {"base_url": base_url} if base_url else {}
        return httpx.AsyncClient(
            timeout=httpx.Timeout(seconds),
            limits=DEFAULT_LIMITS,
            transport=transport,
            follow_redirects=follow_redirects,
            **extra,
        )

    return await client_cache(app).get_or_create(key, build)


async def aclose_all(app: Any | None = None) -> None:
    """Close every governed client held by *app*'s cache (or the global one).

    Idempotent: the second call finds an empty cache and does nothing.
    """
    await client_cache(app).aclose_all()


def reset_process_cache() -> None:
    """Drop the process-wide cache without awaiting anything.

    A synchronous hook exists because the state that needs clearing between
    tests is reachable from teardown that is not itself async, and because a
    client bound to a closed event loop cannot be awaited shut anyway. Prefer
    :func:`aclose_all` wherever a running loop is available.
    """
    _PROCESS_CACHES.clear()


#: Socket timeouts for a governed Redis client, in seconds.
#:
#: redis-py leaves both of these ``None`` by default, which is what made the
#: unauthenticated readiness probe's Redis client unbounded (A02-5). A connect
#: timeout is separate from a read timeout because a wedged host and a wedged
#: server fail differently, and a probe needs to give up on both.
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0


async def governed_redis_client(
    url: str,
    *,
    purpose: str,
    app: Any | None = None,
    socket_timeout: float | None = REDIS_SOCKET_TIMEOUT_SECONDS,
    connect_timeout: float | None = REDIS_CONNECT_TIMEOUT_SECONDS,
) -> Any:
    """Return the shared Redis client for one purpose, with real socket bounds.

    Args:
        url: The ``redis://`` / ``rediss://`` / ``unix://`` URL to dial.
        purpose: Short stable name for the caller; two purposes never share.
        app: Cache anchor; see :func:`client_cache`.
        socket_timeout: Mandatory positive read timeout in seconds.
        connect_timeout: Mandatory positive connect timeout in seconds.

    Returns:
        A client shared with every other caller asking for the same key.

    Raises:
        ValueError: If either timeout is not a positive finite number.

    """
    read_seconds = validate_timeout(socket_timeout, name="socket_timeout")
    connect_seconds = validate_timeout(connect_timeout, name="connect_timeout")
    key = ("redis", purpose, url, read_seconds, connect_seconds)

    async def build() -> Any:
        from redis.asyncio import from_url  # noqa: PLC0415

        return from_url(
            url,
            socket_timeout=read_seconds,
            socket_connect_timeout=connect_seconds,
        )

    return await client_cache(app).get_or_create(key, build)

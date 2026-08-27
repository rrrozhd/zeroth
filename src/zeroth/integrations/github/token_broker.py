"""Installation-token brokerage: caching, single-flight minting, and redaction.

The broker is the only component that holds installation-token values. It
caches one token per ``(installation_id, repository_name)``, refreshes when the
remaining lifetime drops under a margin, single-flights concurrent mints behind
a per-key lock, and -- following the Vault provider's discipline -- retries a
token rejected in use exactly once after a compare-and-clear. Every token ever
minted (and its ``Basic`` credential form) joins a lifetime redaction history
so any text can be scrubbed even after rotation.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.models import (
    InstallationSuspendedError,
    InstallationTokenRejectedError,
)

_REDACTION_MARKER = "[REDACTED:github-installation-token]"
_REFRESH_MARGIN_SECONDS = 120.0
_VERIFY_TTL_SECONDS = 30.0

_T = TypeVar("_T")


def _basic_credential(token: str) -> str:
    """Encode a token the way git's ``http.<url>.extraheader`` carries it."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    return f"Basic {encoded}"


class CredentialLease:
    """A handle on one installation token that never shows the value itself."""

    __slots__ = ("_broker", "_token")

    def __init__(self, broker: InstallationTokenBroker, token: str) -> None:
        self._broker = broker
        self._token = token

    def reveal(self) -> str:
        """Return the raw token value; callers own keeping it out of logs."""
        return self._token

    def basic_auth_header(self) -> str:
        """Return the ``Basic`` Authorization header value for git smart-HTTP."""
        return _basic_credential(self._token)

    def redact(self, text: str) -> str:
        """Scrub every token the broker has ever minted from ``text``."""
        return self._broker.redact(text)

    def __repr__(self) -> str:
        return f"CredentialLease(token={_REDACTION_MARKER})"


class InstallationTokenBroker:
    """Mint, cache, refresh, and redact installation tokens for one App client."""

    def __init__(
        self,
        client: GitHubAppClient,
        *,
        refresh_margin_seconds: float = _REFRESH_MARGIN_SECONDS,
        verify_ttl_seconds: float = _VERIFY_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._refresh_margin = refresh_margin_seconds
        self._verify_ttl = verify_ttl_seconds
        self._clock = clock
        self._monotonic = monotonic
        self._cache: dict[tuple[int, str], tuple[str, float]] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._history: list[str] = []
        self._history_lock = threading.Lock()
        self._verified: dict[int, tuple[float, dict[str, Any]]] = {}
        self._verify_lock = asyncio.Lock()

    # -- leases ----------------------------------------------------------------

    async def lease(self, installation_id: int, repo_name: str) -> CredentialLease:
        """Return a lease on a token with at least the refresh margin remaining.

        Concurrent callers for the same key share one mint; a cached token is
        reused until less than the margin remains. A mint that fails caches
        nothing, and a token already inside the margin at mint time is served
        once but never cached.
        """
        key = (installation_id, repo_name)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            now = self._clock()
            if cached is not None and cached[1] - now > self._refresh_margin:
                return CredentialLease(self, cached[0])
            token, expires_at = await self._client.mint_installation_token(
                installation_id, repo_name
            )
            self._remember(token)
            if expires_at - self._clock() > self._refresh_margin:
                self._cache[key] = (token, expires_at)
            else:
                self._cache.pop(key, None)
            return CredentialLease(self, token)

    def invalidate(self, installation_id: int, repo_name: str, token: str) -> None:
        """Compare-and-clear: drop the cached token only if it is still ``token``.

        A concurrent caller may already have refreshed the cache; clearing
        unconditionally would clobber its fresh token.
        """
        key = (installation_id, repo_name)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == token:
            self._cache.pop(key, None)

    async def run_with_lease(
        self,
        installation_id: int,
        repo_name: str,
        operation: Callable[[str], Awaitable[_T]],
    ) -> _T:
        """Run ``operation(token)`` with exactly one re-mint retry on rejection.

        A 401-in-use invalidates the cached token (compare-and-clear), mints a
        fresh one, and retries once; a second rejection propagates.
        """
        lease = await self.lease(installation_id, repo_name)
        try:
            return await operation(lease.reveal())
        except InstallationTokenRejectedError:
            self.invalidate(installation_id, repo_name, lease.reveal())
            fresh = await self.lease(installation_id, repo_name)
            return await operation(fresh.reveal())

    # -- installation verification ---------------------------------------------

    async def verify_installation(self, installation_id: int) -> dict[str, Any]:
        """Verify the installation is alive and not suspended; memoized briefly.

        A successful verification is cached for the verify TTL on the monotonic
        clock. Revocation and suspension raise and are never cached.

        Raises:
            InstallationRevokedError: The installation no longer exists.
            InstallationSuspendedError: The installation is suspended.
        """
        async with self._verify_lock:
            cached = self._verified.get(installation_id)
            if cached is not None and self._monotonic() - cached[0] < self._verify_ttl:
                return cached[1]
            data = await self._client.get_installation(installation_id)
            if data.get("suspended_at"):
                raise InstallationSuspendedError()
            self._verified[installation_id] = (self._monotonic(), data)
            return data

    # -- redaction --------------------------------------------------------------

    def _remember(self, token: str) -> None:
        """Add a token and its ``Basic`` credential form to the lifetime history."""
        with self._history_lock:
            for value in (token, _basic_credential(token)):
                if value and value not in self._history:
                    self._history.append(value)

    def redact(self, text: str) -> str:
        """Replace every historical token (and credential form) in ``text``."""
        with self._history_lock:
            values = sorted(self._history, key=len, reverse=True)
        for value in values:
            text = text.replace(value, _REDACTION_MARKER)
        return text


__all__ = ["CredentialLease", "InstallationTokenBroker"]

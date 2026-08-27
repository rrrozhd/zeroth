"""Short-lived GitHub App JWTs, signed on demand and cached until near expiry.

The signing key is resolved through the platform secret provider at issue time
and held only for the duration of the signing call; the issued JWT is cached
and reused until less than a minute of validity remains. Issuance is
single-flighted behind an ``asyncio.Lock`` so concurrent callers share one
resolve-and-sign. Neither the PEM nor the JWT is ever logged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import jwt

from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode
from zeroth.platform.secrets.provider import SecretProvider, resolve_secret_async

_ISSUED_AT_BACKDATE_SECONDS = 60
_LIFETIME_SECONDS = 540
_REUSE_MARGIN_SECONDS = 60


class AppJwtIssuer:
    """Issue and cache RS256 GitHub App JWTs for one App identity."""

    def __init__(
        self,
        config: GitHubAppConfig,
        secret_provider: SecretProvider,
        *,
        tenant_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._secret_provider = secret_provider
        self._tenant_id = tenant_id
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached_jwt: str | None = None
        self._cached_expiry: float = 0.0

    async def issue(self) -> str:
        """Return a valid App JWT, signing a fresh one only when needed.

        Raises:
            CheckoutError: With ``config_missing`` when the private key secret
                does not resolve -- fail-closed, never a best-effort fallback.
        """
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh_cached()
            if cached is not None:
                return cached
            pem = await resolve_secret_async(
                self._secret_provider,
                self._config.private_key_secret_name,
                tenant_id=self._tenant_id,
            )
            if not pem:
                raise CheckoutError(
                    CheckoutFailureCode.CONFIG_MISSING,
                    "github app private key secret is not configured",
                )
            now = self._clock()
            claims = {
                "iat": int(now) - _ISSUED_AT_BACKDATE_SECONDS,
                "exp": int(now) + _LIFETIME_SECONDS,
                "iss": self._config.app_id,
            }
            token = jwt.encode(claims, pem, algorithm="RS256")
            self._cached_jwt = token
            self._cached_expiry = int(now) + _LIFETIME_SECONDS
            return token

    def _fresh_cached(self) -> str | None:
        """Return the cached JWT while more than the reuse margin remains."""
        if self._cached_jwt is None:
            return None
        if self._cached_expiry - self._clock() <= _REUSE_MARGIN_SECONDS:
            return None
        return self._cached_jwt


__all__ = ["AppJwtIssuer"]

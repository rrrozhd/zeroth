"""HashiCorp Vault-backed secret provider (WS-F).

Reads secrets from a Vault KV v2 mount at::

    {mount}/data/tenants/{tenant_id}/{logical_name}

The concrete secret is stored under the ``value`` field of that KV entry.

Design notes
------------
* ``resolve``/``resolve_secret`` are the source of truth: they read an
  in-memory TTL cache and, on a miss, perform ONE synchronous ``httpx`` GET
  that fills the cache. This keeps repeat calls cache-served while decoupling
  correctness from bootstrap ordering (a call before :meth:`warm` still works).
* :meth:`warm` is an optional async bulk-prefetch used at bootstrap.
* Missing path -> ``None`` (the caller decides whether to fail closed).
* Secret VALUES are never logged. Error surfaces run through
  :class:`SecretRedactor` and only ever mention the logical name / status code.

Implemented against ``httpx`` (already a core dependency) rather than ``hvac``
so no new heavy dependency is added. Unit-tested with ``httpx.MockTransport``;
NOT exercised against a live Vault (unavailable in this environment).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from zeroth.core.secrets.provider import SecretResolutionError, normalize_secret_name
from zeroth.core.secrets.redaction import SecretRedactor

logger = logging.getLogger(__name__)

_DEFAULT_TENANT = "default"
_SECRET_FIELD = "value"


@dataclass(slots=True)
class _CacheEntry:
    value: str | None
    expires_at: float


class VaultSecretProvider:
    """Resolve secrets from a Vault KV v2 mount with an in-memory TTL cache.

    Parameters
    ----------
    addr:
        Vault base address, e.g. ``https://vault.internal:8200``.
    mount:
        KV v2 mount point (default ``secret``).
    token:
        A Vault token. Either this or an AppRole (role_id + secret_id) is
        required — :func:`build_secret_provider` enforces that at startup.
    role_id / secret_id:
        AppRole credentials; exchanged for a token lazily on first use.
    cache_ttl:
        Seconds a resolved value stays cached before a re-fetch.
    transport:
        Optional ``httpx`` transport (tests inject an ``httpx.MockTransport``).
    """

    def __init__(
        self,
        *,
        addr: str,
        mount: str = "secret",
        token: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        cache_ttl: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._mount = mount.strip("/")
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._cache_ttl = cache_ttl
        self._transport = transport
        self._async_transport = async_transport
        self._timeout = timeout
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        # Redactor is (re)seeded with resolved values so any accidental error
        # surface that echoes a value gets masked.
        self._redactor = SecretRedactor()

    # ------------------------------------------------------------------
    # Public resolution API (sync — source of truth)
    # ------------------------------------------------------------------

    def resolve(self, secret_ref: str, *, tenant_id: str | None = None) -> str | None:
        """Resolve a concrete ref, treated as a logical name under the tenant path."""
        return self.resolve_secret(secret_ref, tenant_id=tenant_id)

    def resolve_many(self, refs: list[str], *, tenant_id: str | None = None) -> dict[str, str]:
        return {
            ref: value
            for ref in refs
            if (value := self.resolve_secret(ref, tenant_id=tenant_id)) is not None
        }

    def resolve_secret(
        self,
        logical_name: str,
        *,
        tenant_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> str | None:
        tenant = tenant_id or _DEFAULT_TENANT
        key = (tenant, logical_name)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.value

        value = self._fetch_sync(tenant, logical_name)
        self._cache[key] = _CacheEntry(value=value, expires_at=now + self._cache_ttl)
        if value is not None:
            # Keep the redactor aware of live values so error paths stay masked.
            self._redactor = SecretRedactor({**self._redactor_known(), key[1]: value})
        return value

    # ------------------------------------------------------------------
    # Optional async bulk prefetch (bootstrap)
    # ------------------------------------------------------------------

    async def warm(self, entries: list[tuple[str, str]] | None = None) -> None:
        """Best-effort async prefetch of ``(tenant_id, logical_name)`` pairs.

        Failures here are swallowed (logged, redacted) — resolution still works
        lazily via :meth:`resolve_secret`. ``entries`` defaults to nothing,
        because Vault KV does not cheaply enumerate arbitrary paths; callers
        that know their key set can pass it to pre-populate the cache.
        """
        if not entries:
            return
        token = await self._ensure_token_async()
        async with self._make_async_client() as client:
            for tenant, logical_name in entries:
                try:
                    value = await self._fetch_async(client, token, tenant, logical_name)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "vault warm failed for %s/%s: %s",
                        tenant,
                        logical_name,
                        self._redactor.redact(str(exc)),
                    )
                    continue
                self._cache[(tenant, logical_name)] = _CacheEntry(
                    value=value, expires_at=time.monotonic() + self._cache_ttl
                )

    # ------------------------------------------------------------------
    # Internal fetch helpers
    # ------------------------------------------------------------------

    def _secret_path(self, tenant: str, logical_name: str) -> str:
        name = normalize_secret_name(logical_name).lower()
        return f"{self._addr}/v1/{self._mount}/data/tenants/{tenant}/{name}"

    def _make_client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=self._timeout)

    def _make_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._async_transport, timeout=self._timeout)

    def _fetch_sync(self, tenant: str, logical_name: str) -> str | None:
        try:
            token = self._ensure_token_sync()
            with self._make_client() as client:
                resp = client.get(
                    self._secret_path(tenant, logical_name),
                    headers={"X-Vault-Token": token},
                )
            return self._extract_value(resp, tenant, logical_name)
        except SecretResolutionError:
            raise
        except Exception as exc:
            logger.warning(
                "vault fetch failed for %s/%s: %s",
                tenant,
                logical_name,
                self._redactor.redact(str(exc)),
            )
            return None

    async def _fetch_async(
        self, client: httpx.AsyncClient, token: str, tenant: str, logical_name: str
    ) -> str | None:
        resp = await client.get(
            self._secret_path(tenant, logical_name),
            headers={"X-Vault-Token": token},
        )
        return self._extract_value(resp, tenant, logical_name)

    def _extract_value(self, resp: httpx.Response, tenant: str, logical_name: str) -> str | None:
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            # Never echo the body — it can contain sensitive material.
            logger.warning(
                "vault returned %s for %s/%s",
                resp.status_code,
                tenant,
                logical_name,
            )
            return None
        try:
            data = resp.json().get("data", {}).get("data", {})
        except Exception:  # pragma: no cover - defensive
            return None
        value = data.get(_SECRET_FIELD)
        return value if isinstance(value, str) and value else None

    # ------------------------------------------------------------------
    # Auth (token or AppRole)
    # ------------------------------------------------------------------

    def _ensure_token_sync(self) -> str:
        if self._token:
            return self._token
        if not (self._role_id and self._secret_id):
            raise SecretResolutionError("vault provider has no token and incomplete AppRole config")
        with self._make_client() as client:
            resp = client.post(
                f"{self._addr}/v1/auth/approle/login",
                json={"role_id": self._role_id, "secret_id": self._secret_id},
            )
        token = self._token_from_login(resp)
        self._token = token
        return token

    async def _ensure_token_async(self) -> str:
        if self._token:
            return self._token
        if not (self._role_id and self._secret_id):
            raise SecretResolutionError("vault provider has no token and incomplete AppRole config")
        async with self._make_async_client() as client:
            resp = await client.post(
                f"{self._addr}/v1/auth/approle/login",
                json={"role_id": self._role_id, "secret_id": self._secret_id},
            )
        token = self._token_from_login(resp)
        self._token = token
        return token

    @staticmethod
    def _token_from_login(resp: httpx.Response) -> str:
        if resp.status_code != 200:
            raise SecretResolutionError(
                f"vault AppRole login failed with status {resp.status_code}"
            )
        token = resp.json().get("auth", {}).get("client_token")
        if not token:
            raise SecretResolutionError("vault AppRole login returned no client_token")
        return token

    def _redactor_known(self) -> dict[str, str]:
        # SecretRedactor keeps its map private; rebuild from cache values.
        return {name: entry.value for (_tenant, name), entry in self._cache.items() if entry.value}


__all__ = ["VaultSecretProvider"]

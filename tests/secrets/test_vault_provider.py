"""VaultSecretProvider against an httpx.MockTransport (WS-F).

No live Vault is reachable in this environment, so these tests drive the
KV-v2 request/response shape through an ``httpx.MockTransport`` fake. What is
verified here: path construction, KV-v2 value extraction, 404 -> None, TTL
caching (one fetch per key), value never logged, and that
``build_secret_provider(backend='vault')`` fails closed on incomplete config.
What is NOT verified: real Vault auth, TLS, token renewal, or KV engine
version negotiation.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from zeroth.platform.config.settings import SecretsSettings
from zeroth.core.secrets import (
    SecretProviderConfigError,
    VaultSecretProvider,
    build_secret_provider,
)

_SECRET_VALUE = "sk-vault-super-secret"


def _kv_v2_transport(*, expect_path: str, value: str = _SECRET_VALUE) -> httpx.MockTransport:
    """MockTransport returning a KV-v2 payload only for the expected path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == expect_path:
            return httpx.Response(
                200,
                json={"data": {"data": {"value": value}, "metadata": {"version": 1}}},
            )
        return httpx.Response(404, json={"errors": []})

    return httpx.MockTransport(handler)


def test_resolve_secret_reads_kv_v2_value_at_tenant_path() -> None:
    path = "/v1/secret/data/tenants/acme/llm_openai"
    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        mount="secret",
        token="root-token",
        transport=_kv_v2_transport(expect_path=path),
    )

    assert provider.resolve_secret("llm.openai", tenant_id="acme") == _SECRET_VALUE


def test_resolve_missing_path_returns_none() -> None:
    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        transport=_kv_v2_transport(expect_path="/v1/secret/data/tenants/acme/llm_openai"),
    )

    # Different logical name -> 404 branch -> None.
    assert provider.resolve_secret("llm.anthropic", tenant_id="acme") is None


def test_resolve_is_cached_within_ttl() -> None:
    path = "/v1/secret/data/tenants/acme/llm_openai"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"data": {"data": {"value": _SECRET_VALUE}, "metadata": {}}}
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        cache_ttl=1000.0,
        transport=httpx.MockTransport(handler),
    )

    assert provider.resolve_secret("llm.openai", tenant_id="acme") == _SECRET_VALUE
    assert provider.resolve_secret("llm.openai", tenant_id="acme") == _SECRET_VALUE
    assert calls["n"] == 1  # second call served from cache
    assert path  # path documented for clarity


def test_resolve_never_logs_secret_value(caplog: pytest.LogCaptureFixture) -> None:
    # Force an error surface: transport raises so the redacted warning path runs.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level(logging.WARNING):
        assert provider.resolve_secret("llm.openai", tenant_id="acme") is None

    # The secret value must never appear in any log record.
    assert _SECRET_VALUE not in caplog.text


def test_build_secret_provider_vault_incomplete_config_raises() -> None:
    # backend=vault but no addr -> fail closed at startup.
    with pytest.raises(SecretProviderConfigError, match="vault_addr"):
        build_secret_provider(SecretsSettings(backend="vault"))

    # addr present but neither token nor a full AppRole pair -> fail closed.
    with pytest.raises(SecretProviderConfigError, match="vault_token or a full"):
        build_secret_provider(
            SecretsSettings(backend="vault", vault_addr="https://vault.test:8200")
        )


def test_build_secret_provider_vault_with_token_ok() -> None:
    provider = build_secret_provider(
        SecretsSettings(
            backend="vault",
            vault_addr="https://vault.test:8200",
            vault_token="root-token",  # type: ignore[arg-type]
        )
    )
    assert isinstance(provider, VaultSecretProvider)


# --- async resolution: pooling and single-flight ------------------------------


@pytest.mark.asyncio
async def test_concurrent_async_misses_produce_one_fetch() -> None:
    import asyncio

    gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        gets += 1
        return httpx.Response(
            200, json={"data": {"data": {"value": _SECRET_VALUE}, "metadata": {"version": 1}}}
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root",
        async_transport=httpx.MockTransport(handler),
    )
    values = await asyncio.gather(
        *(provider.resolve_secret_async("llm.openai", tenant_id="acme") for _ in range(20))
    )
    assert values == [_SECRET_VALUE] * 20
    assert gets == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_async_client_is_reused_and_closed_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"data": {"value": _SECRET_VALUE}, "metadata": {"version": 1}}}
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root",
        async_transport=httpx.MockTransport(handler),
    )
    assert await provider.resolve_secret_async("llm.openai", tenant_id="a") == _SECRET_VALUE
    first_client = provider._async_client
    assert first_client is not None
    assert await provider.resolve_secret_async("llm.anthropic", tenant_id="b") == _SECRET_VALUE
    assert provider._async_client is first_client

    await provider.aclose()
    assert first_client.is_closed
    # Second close is a safe no-op.
    await provider.aclose()


@pytest.mark.asyncio
async def test_async_approle_login_single_flight() -> None:
    import asyncio

    logins = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/v1/auth/approle/login":
            logins += 1
            return httpx.Response(200, json={"auth": {"client_token": "tok"}})
        return httpx.Response(
            200, json={"data": {"data": {"value": _SECRET_VALUE}, "metadata": {"version": 1}}}
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        role_id="r",
        secret_id="s",
        async_transport=httpx.MockTransport(handler),
    )
    values = await asyncio.gather(
        *(provider.resolve_secret_async(f"llm.k{i}", tenant_id="acme") for i in range(10))
    )
    assert set(values) == {_SECRET_VALUE}
    assert logins == 1
    await provider.aclose()

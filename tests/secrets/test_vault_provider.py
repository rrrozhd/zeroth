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
import threading

import httpx
import pytest

import zeroth.platform.secrets.vault as vault_module

from zeroth.platform.config.settings import SecretsSettings
from zeroth.platform.secrets import (
    SecretProviderConfigError,
    VaultSecretProvider,
    build_secret_provider,
)

_SECRET_VALUE = "sk-vault-super-secret"


def test_concurrent_secret_history_publication_cannot_lose_a_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VaultSecretProvider(addr="https://vault.test", token="root-token")
    original_redactor = vault_module.SecretRedactor
    first_snapshot_ready = threading.Event()
    release_first = threading.Event()

    def gated_redactor(seeds=None):  # noqa: ANN001, ANN202
        snapshot = list(seeds or ())
        if len(snapshot) == 1:
            first_snapshot_ready.set()
            assert release_first.wait(timeout=2)
        return original_redactor(snapshot)

    monkeypatch.setattr(vault_module, "SecretRedactor", gated_redactor)
    first = threading.Thread(
        target=provider._remember_secret,
        args=(("tenant-a", "shared"), "tenant-a-secret"),
    )
    second = threading.Thread(
        target=provider._remember_secret,
        args=(("tenant-b", "shared"), "tenant-b-secret"),
    )

    first.start()
    assert first_snapshot_ready.wait(timeout=2)
    second.start()
    second.join(timeout=0.2)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert {reference for reference, _value in provider._redaction_history} == {
        ("tenant-a", "shared"),
        ("tenant-b", "shared"),
    }
    assert provider._redactor.redact("tenant-a-secret tenant-b-secret") == (
        "[REDACTED:SECRET] [REDACTED:SECRET]"
    )


@pytest.mark.asyncio
async def test_warm_without_credentials_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = VaultSecretProvider(addr="x")

    with caplog.at_level(logging.WARNING):
        await provider.warm([("tenant-safe", "logical-safe")])

    assert caplog.messages == [
        "vault warm setup failed: vault provider has no token and incomplete AppRole config"
    ]


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


@pytest.mark.parametrize("resolution_order", [("tenant-a", "tenant-b"), ("tenant-b", "tenant-a")])
def test_redactor_preserves_same_name_secret_identity_across_tenants(
    resolution_order: tuple[str, str],
) -> None:
    values = {
        "/v1/secret/data/tenants/tenant-a/shared_token": "shared-prefix",
        "/v1/secret/data/tenants/tenant-b/shared_token": "shared-prefix-suffix",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"data": {"value": values[request.url.path]}, "metadata": {}}},
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        transport=httpx.MockTransport(handler),
    )

    for tenant in resolution_order:
        assert (
            provider.resolve_secret("shared.token", tenant_id=tenant)
            == {
                "tenant-a": "shared-prefix",
                "tenant-b": "shared-prefix-suffix",
            }[tenant]
        )
    redacted = provider._redactor.redact("shared-prefix shared-prefix-suffix")
    assert redacted == "[REDACTED:SECRET] [REDACTED:SECRET]"


@pytest.mark.asyncio
async def test_warm_preserves_same_name_secret_identity_across_tenants() -> None:
    values = {
        "/v1/secret/data/tenants/tenant-a/shared_token": "tenant-a-secret",
        "/v1/secret/data/tenants/tenant-b/shared_token": "tenant-b-secret",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"data": {"value": values[request.url.path]}, "metadata": {}}},
        )

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        async_transport=httpx.MockTransport(handler),
    )

    await provider.warm([("tenant-a", "shared.token"), ("tenant-b", "shared.token")])

    assert provider._redactor.redact("tenant-a-secret tenant-b-secret") == (
        "[REDACTED:SECRET] [REDACTED:SECRET]"
    )
    await provider.aclose()


@pytest.mark.asyncio
async def test_warm_redacts_earlier_value_from_later_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"data": {"data": {"value": "first-secret"}}})
        raise RuntimeError("transport echoed first-secret")

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        async_transport=httpx.MockTransport(handler),
    )

    with caplog.at_level(logging.WARNING):
        await provider.warm([("tenant-a", "first"), ("tenant-a", "second")])

    assert "first-secret" not in caplog.text
    assert "[REDACTED:SECRET]" in caplog.text
    await provider.aclose()


def test_redactor_retains_rotated_secret_values_after_cache_expiry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = iter(["old-secret", "new-secret"])

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            value = next(responses)
        except StopIteration:
            raise RuntimeError("transport echoed old-secret") from None
        return httpx.Response(200, json={"data": {"data": {"value": value}}})

    provider = VaultSecretProvider(
        addr="https://vault.test:8200",
        token="root-token",
        cache_ttl=0,
        transport=httpx.MockTransport(handler),
    )

    assert provider.resolve_secret("rotating", tenant_id="tenant-a") == "old-secret"
    assert provider.resolve_secret("rotating", tenant_id="tenant-a") == "new-secret"
    with caplog.at_level(logging.WARNING):
        assert provider.resolve_secret("rotating", tenant_id="tenant-a") is None

    assert "old-secret" not in caplog.text
    assert "[REDACTED:SECRET]" in caplog.text


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


def test_approle_token_reauth_on_expiry_retries_and_refreshes() -> None:
    # B12: when a cached AppRole token is rejected (403) on a secret GET, the
    # provider must invalidate it, re-login, and retry the GET once — not reuse
    # the stale token forever and return None.
    state = {"logins": 0, "secret_gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/approle/login"):
            state["logins"] += 1
            return httpx.Response(200, json={"auth": {"client_token": f"token-{state['logins']}"}})
        state["secret_gets"] += 1
        token = request.headers.get("X-Vault-Token")
        if state["secret_gets"] == 1:
            return httpx.Response(200, json={"data": {"data": {"value": "s3cr3t"}}})
        # The stale token-1 is now rejected; a retry with the refreshed token-2 works.
        if token == "token-1":
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json={"data": {"data": {"value": "s3cr3t-2"}}})

    provider = VaultSecretProvider(
        addr="https://vault.internal:8200",
        role_id="role-1",
        secret_id="secret-1",
        cache_ttl=0.0,  # force a re-fetch on the second call
        transport=httpx.MockTransport(handler),
    )

    assert provider.resolve_secret("llm.openai", tenant_id="acme") == "s3cr3t"
    assert provider._token == "token-1"

    # Second fetch: token-1 -> 403 -> re-auth to token-2 -> retry succeeds.
    assert provider.resolve_secret("llm.openai", tenant_id="acme") == "s3cr3t-2"
    assert provider._token == "token-2"  # refreshed, not the stale token
    assert state["logins"] == 2


def test_static_token_auth_failure_is_not_cached() -> None:
    # B12: a static injected token can't be refreshed, so a 403 yields None — but
    # that None must NOT be cached, else a later (recovered) call is masked.
    responses = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        responses["n"] += 1
        if responses["n"] == 1:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json={"data": {"data": {"value": "recovered"}}})

    provider = VaultSecretProvider(
        addr="https://vault.internal:8200",
        token="static-token",
        cache_ttl=300.0,  # long TTL: proves the None was NOT cached
        transport=httpx.MockTransport(handler),
    )

    assert provider.resolve_secret("llm.openai", tenant_id="acme") is None
    assert provider._token == "static-token"  # static token left intact
    # A second call re-fetches (the auth-failure None was not cached) and succeeds.
    assert provider.resolve_secret("llm.openai", tenant_id="acme") == "recovered"

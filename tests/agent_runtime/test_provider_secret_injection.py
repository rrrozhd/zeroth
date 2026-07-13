"""LiteLLMProviderAdapter secret injection + cache re-key (WS-F).

These prove the LLM-key leak is closed: the api_key is injected into the
ChatLiteLLM client (and OVERRIDES the ambient env var), the client cache is
keyed by tenant + key fingerprint (a rotated key never reuses a stale client),
and a fail-closed adapter raises instead of silently reading process env.
"""

from __future__ import annotations

import pytest

from zeroth.core.agent_runtime.provider import LiteLLMProviderAdapter
from zeroth.core.secrets import SecretResolutionError


class _FakeSecretProvider:
    """Returns a fixed key (or a rotating one) for every logical name."""

    def __init__(self, key: str | None = "sk-injected", *, rotate: bool = False) -> None:
        self._key = key
        self._rotate = rotate
        self._calls = 0

    def resolve(self, secret_ref: str, *, tenant_id: str | None = None) -> str | None:
        return self.resolve_secret(secret_ref, tenant_id=tenant_id)

    def resolve_many(self, refs, *, tenant_id=None):  # pragma: no cover - unused here
        return {r: v for r in refs if (v := self.resolve_secret(r, tenant_id=tenant_id))}

    def resolve_secret(
        self, logical_name: str, *, tenant_id: str | None = None, deployment_ref=None
    ) -> str | None:
        self._calls += 1
        if self._key is None:
            return None
        if self._rotate:
            return f"{self._key}-{self._calls}"
        return self._key


def test_injected_key_overrides_env_named_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ambient env would otherwise win via the ChatLiteLLM validator -> named field.
    monkeypatch.setenv("OPENAI_API_KEY", "env-should-lose")

    adapter = LiteLLMProviderAdapter(
        secret_provider=_FakeSecretProvider("sk-injected"),
        tenant_id="acme",
    )
    client = adapter._get_client("openai/gpt-4o")

    # The per-provider named field is the one that leaks the env value; assert
    # our injected key overrode it (not merely that the generic api_key is set).
    assert client.openai_api_key == "sk-injected"
    assert client.api_key == "sk-injected"


def test_cache_rekeys_on_key_rotation() -> None:
    # Same adapter, same model, but the secret provider returns a new key each
    # call -> two distinct cached clients (proves the fingerprint dimension).
    adapter = LiteLLMProviderAdapter(
        secret_provider=_FakeSecretProvider("sk", rotate=True),
        tenant_id="acme",
    )

    adapter._get_client("openai/gpt-4o")
    adapter._get_client("openai/gpt-4o")

    assert len(adapter._clients) == 2
    # Cache keys carry model + tenant + fingerprint, never the raw key.
    for model, tenant, fp in adapter._clients:
        assert model == "openai/gpt-4o"
        assert tenant == "acme"
        assert "sk-" not in fp  # fingerprint is a hash, not the key


def test_stable_key_reuses_single_client() -> None:
    adapter = LiteLLMProviderAdapter(
        secret_provider=_FakeSecretProvider("sk-stable"),
        tenant_id="acme",
    )
    c1 = adapter._get_client("openai/gpt-4o")
    c2 = adapter._get_client("openai/gpt-4o")
    assert c1 is c2
    assert len(adapter._clients) == 1


def test_fail_closed_missing_key_raises_not_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-present-but-must-not-be-used")

    adapter = LiteLLMProviderAdapter(
        secret_provider=_FakeSecretProvider(None),  # resolves to None
        tenant_id="acme",
        allow_env_fallback=False,
    )
    with pytest.raises(SecretResolutionError, match="llm.openai"):
        adapter._get_client("openai/gpt-4o")


def test_env_fallback_allowed_builds_client_without_injected_key() -> None:
    # allow_env_fallback=True (default) + missing key -> no injection, LiteLLM
    # keeps its own env resolution (original behaviour preserved).
    adapter = LiteLLMProviderAdapter(
        secret_provider=_FakeSecretProvider(None),
        tenant_id="acme",
        allow_env_fallback=True,
    )
    client = adapter._get_client("openai/gpt-4o")
    assert client is not None
    # No key injected -> fingerprint slot is the 'env' sentinel.
    assert list(adapter._clients)[0][2] == "env"


def test_no_secret_provider_preserves_legacy_behaviour() -> None:
    # Back-compat: constructing with no secret provider still works and caches
    # by model under the 'env' fingerprint.
    adapter = LiteLLMProviderAdapter()
    client = adapter._get_client("anthropic/claude-sonnet-4-5")
    assert client is not None
    assert list(adapter._clients)[0] == ("anthropic/claude-sonnet-4-5", None, "env")


def test_llm_key_map_overrides_logical_name() -> None:
    captured: dict[str, str | None] = {}

    class _Recorder(_FakeSecretProvider):
        def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
            captured["logical"] = logical_name
            return super().resolve_secret(logical_name, tenant_id=tenant_id)

    adapter = LiteLLMProviderAdapter(
        secret_provider=_Recorder("sk"),
        tenant_id="acme",
        llm_key_map={"openai": "llm.openai_prod"},
    )
    adapter._get_client("openai/gpt-4o")
    assert captured["logical"] == "llm.openai_prod"


# --- async resolution path (WS: MCP/Vault hardening) --------------------------


class _AsyncOnlySecretProvider(_FakeSecretProvider):
    """Fails loudly if the sync resolution path is used from async code."""

    def resolve_secret(
        self, logical_name: str, *, tenant_id: str | None = None, deployment_ref=None
    ) -> str | None:
        raise AssertionError("sync resolve_secret used on an async path")

    async def resolve_secret_async(
        self, logical_name: str, *, tenant_id: str | None = None, deployment_ref=None
    ) -> str | None:
        return "sk-async"


@pytest.mark.asyncio
async def test_get_client_async_uses_async_resolution() -> None:
    adapter = LiteLLMProviderAdapter(
        secret_provider=_AsyncOnlySecretProvider(), tenant_id="acme"
    )
    client = await adapter._get_client_async("openai/gpt-4o")
    assert client.api_key == "sk-async"


@pytest.mark.asyncio
async def test_ainvoke_resolves_key_without_sync_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage
    from langchain_litellm import ChatLiteLLM

    from zeroth.core.agent_runtime.provider import ProviderRequest

    async def _fake_ainvoke(self, messages, **kwargs):  # noqa: ANN001
        return AIMessage(content='{"ok": true}')

    monkeypatch.setattr(ChatLiteLLM, "ainvoke", _fake_ainvoke)
    adapter = LiteLLMProviderAdapter(
        secret_provider=_AsyncOnlySecretProvider(), tenant_id="acme"
    )
    response = await adapter.ainvoke(
        ProviderRequest(
            model_name="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
    )
    assert response.content == '{"ok": true}'

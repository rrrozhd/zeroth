from __future__ import annotations

import pytest

from zeroth.integrations.execution import EnvironmentVariable
from zeroth.platform.secrets import EnvSecretProvider, SecretRedactor, SecretResolver


def test_env_secret_provider_resolves_refs_from_environment() -> None:
    provider = EnvSecretProvider({"API_KEY": "secret-value", "TOKEN": "token-value"})

    assert provider.resolve("API_KEY") == "secret-value"
    assert provider.resolve_many(["API_KEY", "MISSING", "TOKEN"]) == {
        "API_KEY": "secret-value",
        "TOKEN": "token-value",
    }


def test_secret_resolver_replaces_secret_refs_with_values() -> None:
    resolver = SecretResolver(EnvSecretProvider({"API_KEY": "secret-value"}))

    resolved = resolver.resolve_environment_variables(
        [
            EnvironmentVariable(name="API_KEY", secret_ref="API_KEY"),
            EnvironmentVariable(name="PLAIN", value="visible"),
        ]
    )

    assert resolved == {"API_KEY": "secret-value", "PLAIN": "visible"}
    assert resolver.known_secrets() == {"API_KEY": "secret-value"}


def test_secret_resolver_raises_for_missing_secret_refs() -> None:
    resolver = SecretResolver(EnvSecretProvider({}))

    with pytest.raises(KeyError, match="missing secret"):
        resolver.resolve_environment_variables(
            [EnvironmentVariable(name="API_KEY", secret_ref="API_KEY")]
        )


def test_secret_redactor_masks_known_values_in_dicts_and_strings() -> None:
    redactor = SecretRedactor({"API_KEY": "super-secret", "TOKEN": "token-123"})

    assert redactor.redact("Authorization: super-secret") == "Authorization: [REDACTED:API_KEY]"
    assert redactor.redact(
        {
            "nested": {"token": "token-123"},
            "message": "super-secret token-123",
        }
    ) == {
        "nested": {"token": "[REDACTED:TOKEN]"},
        "message": "[REDACTED:API_KEY] [REDACTED:TOKEN]",
    }


@pytest.mark.parametrize(
    "known",
    [
        {
            ("tenant-b", "short"): "tenant-a",
            ("tenant-a", "long"): "tenant-a-secret",
        },
        {
            ("tenant-a", "long"): "tenant-a-secret",
            ("tenant-b", "short"): "tenant-a",
        },
    ],
)
def test_secret_redactor_uses_one_longest_match_pass_with_opaque_tenant_markers(
    known: dict[tuple[str, str], str],
) -> None:
    redactor = SecretRedactor(known)

    assert redactor.redact("tenant-a tenant-a-secret") == "[REDACTED:SECRET] [REDACTED:SECRET]"


@pytest.mark.parametrize(
    "known",
    [
        {
            ("tenant-b", "shared"): "duplicate-secret",
            ("tenant-a", "shared"): "duplicate-secret",
        },
        {
            ("tenant-a", "shared"): "duplicate-secret",
            ("tenant-b", "shared"): "duplicate-secret",
        },
    ],
)
def test_secret_redactor_deduplicates_equal_tenant_values_without_identity_leakage(
    known: dict[tuple[str, str], str],
) -> None:
    redactor = SecretRedactor(known)

    assert redactor.redact("duplicate-secret") == "[REDACTED:SECRET]"


def test_secret_redactor_normalizes_string_aliases_before_deduplicating_values() -> None:
    redactor = SecretRedactor({"alias.one": "shared", "alias-two": "shared"})

    assert redactor.redact("shared") == "[REDACTED:ALIAS_ONE]"


# --- async compatibility helpers --------------------------------------------


class _AsyncNativeProvider:
    """Async-native provider: sync methods must never be called."""

    def resolve(self, secret_ref, *, tenant_id=None):
        raise AssertionError("sync resolve called on async-native provider")

    def resolve_many(self, refs, *, tenant_id=None):
        raise AssertionError("sync resolve_many called on async-native provider")

    def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
        raise AssertionError("sync resolve_secret called on async-native provider")

    async def resolve_async(self, secret_ref, *, tenant_id=None):
        return f"async:{secret_ref}"

    async def resolve_many_async(self, refs, *, tenant_id=None):
        return {ref: f"async:{ref}" for ref in refs}

    async def resolve_secret_async(self, logical_name, *, tenant_id=None, deployment_ref=None):
        return f"async:{logical_name}:{tenant_id}"


class _BlockingSyncProvider:
    """Sync-only provider that blocks; must run off the event loop."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def resolve(self, secret_ref, *, tenant_id=None):
        import time

        time.sleep(self._delay)
        return f"sync:{secret_ref}"

    def resolve_many(self, refs, *, tenant_id=None):
        return {ref: self.resolve(ref) for ref in refs}

    def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
        import time

        time.sleep(self._delay)
        return f"sync:{logical_name}"


@pytest.mark.asyncio
async def test_async_helpers_await_native_async_provider() -> None:
    from zeroth.platform.secrets.provider import (
        resolve_async,
        resolve_many_async,
        resolve_secret_async,
    )

    provider = _AsyncNativeProvider()
    assert await resolve_async(provider, "KEY") == "async:KEY"
    assert await resolve_many_async(provider, ["A", "B"]) == {"A": "async:A", "B": "async:B"}
    assert await resolve_secret_async(provider, "llm.openai", tenant_id="t1") == (
        "async:llm.openai:t1"
    )


@pytest.mark.asyncio
async def test_env_provider_resolves_natively_async() -> None:
    from zeroth.platform.secrets.provider import resolve_async, resolve_secret_async

    provider = EnvSecretProvider({"OPENAI_API_KEY": "sk-1", "LLM_OPENAI": "sk-2"})
    assert await provider.resolve_async("OPENAI_API_KEY") == "sk-1"
    assert await provider.resolve_secret_async("llm.openai") == "sk-2"
    assert await resolve_async(provider, "OPENAI_API_KEY") == "sk-1"
    assert await resolve_secret_async(provider, "llm.openai") == "sk-2"


@pytest.mark.asyncio
async def test_sync_only_provider_does_not_block_event_loop() -> None:
    import asyncio

    from zeroth.platform.secrets.provider import resolve_secret_async

    heartbeats = 0

    async def _heartbeat() -> None:
        nonlocal heartbeats
        while True:
            heartbeats += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(_heartbeat())
    try:
        value = await resolve_secret_async(_BlockingSyncProvider(0.2), "llm.openai")
    finally:
        task.cancel()
    assert value == "sync:llm.openai"
    # A blocked loop would have frozen the heartbeat for the full 200ms.
    assert heartbeats >= 5


@pytest.mark.asyncio
async def test_secret_resolver_resolves_environment_variables_async() -> None:
    class _AsyncOnlyManyProvider:
        def resolve(self, secret_ref, *, tenant_id=None):
            raise AssertionError("sync resolve used on an async path")

        def resolve_many(self, refs, *, tenant_id=None):
            raise AssertionError("sync resolve_many used on an async path")

        def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
            raise AssertionError("sync resolve_secret used on an async path")

        async def resolve_many_async(self, refs, *, tenant_id=None):
            return {ref: f"v-{ref}" for ref in refs}

    resolver = SecretResolver(_AsyncOnlyManyProvider())
    resolved = await resolver.resolve_environment_variables_async(
        [
            EnvironmentVariable(name="API_KEY", secret_ref="ref-a"),
            EnvironmentVariable(name="PLAIN", value="x"),
        ]
    )
    assert resolved == {"API_KEY": "v-ref-a", "PLAIN": "x"}
    assert resolver.known_secrets() == {"ref-a": "v-ref-a"}

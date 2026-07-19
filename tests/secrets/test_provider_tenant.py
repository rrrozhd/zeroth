"""Tenant-scoped resolution on EnvSecretProvider + fail-closed behaviour (WS-F)."""

from __future__ import annotations

import pytest

from zeroth.platform.secrets import (
    EnvSecretProvider,
    SecretResolutionError,
    normalize_secret_name,
)


def test_resolve_secret_prefers_tenant_scoped_env_name() -> None:
    provider = EnvSecretProvider(
        {
            "ZEROTH_SECRET__ACME__LLM_OPENAI": "tenant-key",
            "LLM_OPENAI": "bare-key",
        }
    )

    assert provider.resolve_secret("llm.openai", tenant_id="acme") == "tenant-key"


def test_resolve_secret_falls_back_to_bare_name() -> None:
    provider = EnvSecretProvider({"LLM_OPENAI": "bare-key"})

    # No tenant-scoped var present -> falls back to the bare logical env name.
    assert provider.resolve_secret("llm.openai", tenant_id="acme") == "bare-key"
    assert provider.resolve_secret("llm.openai") == "bare-key"


def test_resolve_secret_missing_returns_none() -> None:
    provider = EnvSecretProvider({})

    assert provider.resolve_secret("llm.anthropic", tenant_id="acme") is None


def test_fail_closed_caller_raises_on_missing_secret() -> None:
    """A fail-closed caller turns an unresolved secret into a hard error."""
    provider = EnvSecretProvider({})

    def resolve_or_fail(name: str) -> str:
        value = provider.resolve_secret(name, tenant_id="acme")
        if value is None:
            raise SecretResolutionError(f"no secret for {name!r}")
        return value

    with pytest.raises(SecretResolutionError, match="llm.openai"):
        resolve_or_fail("llm.openai")


def test_resolve_ref_tenant_scoped_then_bare_backcompat() -> None:
    """The ref-based resolve keeps http-client back-compat: bare ref still hits."""
    provider = EnvSecretProvider(
        {
            "ZEROTH_SECRET__ACME__STRIPE_KEY": "scoped",
            "stripe_key": "bare-ref-value",
        }
    )

    # Tenant-scoped override wins when tenant_id is given.
    assert provider.resolve("stripe_key", tenant_id="acme") == "scoped"
    # Without a tenant, the exact bare ref string is used (unchanged behaviour).
    assert provider.resolve("stripe_key") == "bare-ref-value"


def test_normalize_secret_name() -> None:
    assert normalize_secret_name("llm.openai") == "LLM_OPENAI"
    assert normalize_secret_name("signing.deployment") == "SIGNING_DEPLOYMENT"
    assert normalize_secret_name("my-endpoint-key") == "MY_ENDPOINT_KEY"

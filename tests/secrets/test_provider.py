from __future__ import annotations

import pytest

from zeroth.core.execution_units import EnvironmentVariable
from zeroth.core.secrets import EnvSecretProvider, SecretRedactor, SecretResolver


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

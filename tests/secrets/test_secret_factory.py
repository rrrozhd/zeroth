"""build_secret_provider backend selection (WS-F)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.platform.config.settings import SecretsSettings
from zeroth.platform.secrets import (
    EnvSecretProvider,
    VaultSecretProvider,
    build_secret_provider,
)


def test_default_backend_is_env() -> None:
    assert SecretsSettings().backend == "env"
    provider = build_secret_provider(SecretsSettings())
    assert isinstance(provider, EnvSecretProvider)


def test_env_backend_selects_env_provider() -> None:
    provider = build_secret_provider(SecretsSettings(backend="env"))
    assert isinstance(provider, EnvSecretProvider)


def test_vault_backend_selects_vault_provider() -> None:
    provider = build_secret_provider(
        SecretsSettings(
            backend="vault",
            vault_addr="https://vault.test:8200",
            vault_token="tok",  # type: ignore[arg-type]
        )
    )
    assert isinstance(provider, VaultSecretProvider)


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValidationError, match="Input should be 'env' or 'vault'"):
        SecretsSettings(backend="consul")

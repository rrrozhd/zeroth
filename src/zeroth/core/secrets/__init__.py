"""Secret provider, resolution, and redaction primitives."""

from zeroth.core.secrets.factory import SecretProviderConfigError, build_secret_provider
from zeroth.core.secrets.provider import (
    EnvSecretProvider,
    SecretProvider,
    SecretResolutionError,
    SecretResolver,
    normalize_secret_name,
)
from zeroth.core.secrets.redaction import SecretRedactor
from zeroth.core.secrets.vault import VaultSecretProvider

__all__ = [
    "EnvSecretProvider",
    "SecretProvider",
    "SecretProviderConfigError",
    "SecretRedactor",
    "SecretResolutionError",
    "SecretResolver",
    "VaultSecretProvider",
    "build_secret_provider",
    "normalize_secret_name",
]

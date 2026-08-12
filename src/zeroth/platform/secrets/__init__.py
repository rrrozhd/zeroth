"""Secret provider, resolution, and redaction primitives."""

from zeroth.platform.secrets.factory import SecretProviderConfigError, build_secret_provider
from zeroth.platform.secrets.provider import (
    EnvSecretProvider,
    SecretProvider,
    SecretResolutionError,
    SecretResolver,
    normalize_secret_name,
    resolve_async,
    resolve_many_async,
    resolve_secret_async,
)
from zeroth.platform.secrets.redaction import SecretRedactor
from zeroth.platform.secrets.vault import TenantScopedVaultDriver, VaultSecretProvider

__all__ = [
    "EnvSecretProvider",
    "SecretProvider",
    "SecretProviderConfigError",
    "SecretRedactor",
    "SecretResolutionError",
    "SecretResolver",
    "TenantScopedVaultDriver",
    "VaultSecretProvider",
    "build_secret_provider",
    "normalize_secret_name",
    "resolve_async",
    "resolve_many_async",
    "resolve_secret_async",
]

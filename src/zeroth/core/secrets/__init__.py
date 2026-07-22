"""Legacy import path for the platform secrets package.

Secret resolution lives in :mod:`zeroth.platform.secrets`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.platform.secrets import (
    EnvSecretProvider,
    SecretProvider,
    SecretProviderConfigError,
    SecretRedactor,
    SecretResolutionError,
    SecretResolver,
    VaultSecretProvider,
    build_secret_provider,
    normalize_secret_name,
    resolve_async,
    resolve_many_async,
    resolve_secret_async,
)

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
    "resolve_async",
    "resolve_many_async",
    "resolve_secret_async",
]

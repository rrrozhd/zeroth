"""Legacy import path for :mod:`zeroth.platform.secrets.provider`."""

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

__all__ = [
    "EnvSecretProvider",
    "SecretProvider",
    "SecretResolutionError",
    "SecretResolver",
    "normalize_secret_name",
    "resolve_async",
    "resolve_many_async",
    "resolve_secret_async",
]

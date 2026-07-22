"""Legacy import path for :mod:`zeroth.platform.secrets.factory`."""

from zeroth.platform.secrets.factory import (
    SecretProviderConfigError,
    build_secret_provider,
)

__all__ = [
    "SecretProviderConfigError",
    "build_secret_provider",
]

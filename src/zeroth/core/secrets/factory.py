"""Build the process-wide :class:`SecretProvider` from settings (WS-F).

One provider is constructed per process and reused for every secret surface:
LLM keys, the WS-D signing key, HTTP-client auth headers, and execution-unit
env resolution. Selecting ``backend='vault'`` with incomplete connection/auth
config raises at startup (fail-closed) rather than silently degrading.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from zeroth.core.secrets.provider import EnvSecretProvider, SecretProvider

if TYPE_CHECKING:
    from zeroth.core.config.settings import SecretsSettings


class SecretProviderConfigError(RuntimeError):
    """Secret-backend configuration is incomplete or invalid."""


def build_secret_provider(settings: SecretsSettings) -> SecretProvider:
    """Return the single secret provider selected by ``settings.backend``.

    * ``env`` (dev default) — reads process environment variables, with the
      ``ZEROTH_SECRET__{TENANT}__{NAME}`` tenant-scoping convention.
    * ``vault`` — reads a HashiCorp Vault KV-v2 mount. Requires ``vault_addr``
      plus either a ``vault_token`` or a full AppRole pair
      (``vault_role_id`` + ``vault_secret_id``); otherwise raises.
    """
    backend = (settings.backend or "env").lower()

    if backend == "env":
        return EnvSecretProvider(os.environ)

    if backend == "vault":
        from zeroth.core.secrets.vault import VaultSecretProvider

        if not settings.vault_addr:
            raise SecretProviderConfigError(
                "secrets.backend='vault' requires secrets.vault_addr to be set"
            )
        token = settings.vault_token.get_secret_value() if settings.vault_token else None
        role_id = settings.vault_role_id.get_secret_value() if settings.vault_role_id else None
        secret_id = (
            settings.vault_secret_id.get_secret_value() if settings.vault_secret_id else None
        )
        has_approle = bool(role_id and secret_id)
        if not token and not has_approle:
            raise SecretProviderConfigError(
                "secrets.backend='vault' requires either vault_token or a full "
                "AppRole pair (vault_role_id + vault_secret_id)"
            )
        return VaultSecretProvider(
            addr=settings.vault_addr,
            mount=settings.vault_mount,
            token=token,
            role_id=role_id,
            secret_id=secret_id,
            cache_ttl=settings.vault_cache_ttl,
        )

    raise SecretProviderConfigError(
        f"unknown secrets.backend {settings.backend!r} (expected 'env' or 'vault')"
    )


__all__ = ["SecretProviderConfigError", "build_secret_provider"]

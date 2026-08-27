"""Runtime application of persisted memory connector configurations.

Shared by bootstrap (re-registering persisted connectors at startup) and the
connector management API (registering newly created/updated connectors into
the live registry).
"""

from __future__ import annotations

import logging
from typing import Any

from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.memory.factory import build_connector
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry

logger = logging.getLogger(__name__)


def apply_config(
    registry: InMemoryConnectorRegistry,
    ref: str,
    backend_type: str,
    params: dict[str, Any],
    *,
    secret_provider: Any | None = None,
    tenant_id: str | None = None,
    allow_env_fallback: bool = True,
) -> tuple[ConnectorManifest, Any]:
    """Build a connector from a saved config and register it under ``ref``.

    Raises ValueError when the config cannot be built (unknown backend,
    missing params, or missing optional dependency).
    """
    manifest, connector = build_connector(
        backend_type,
        params,
        secret_provider=secret_provider,
        tenant_id=tenant_id,
        allow_env_fallback=allow_env_fallback,
    )
    registry.register(ref, manifest, connector)
    return manifest, connector


async def load_persisted_connectors(
    registry: InMemoryConnectorRegistry,
    repository: MemoryConnectorConfigRepository,
    *,
    tenant_id: str | None = None,
    log: logging.Logger | None = None,
    secret_provider: Any | None = None,
    allow_env_fallback: bool = True,
) -> list[str]:
    """Register every persisted connector config into the registry.

    A bad saved config (missing dependency, invalid params) is logged and
    skipped -- it must never break boot. Returns the refs that loaded.

    WS-B: a deployment is tenant-pinned, so pass its ``tenant_id`` to load
    only that tenant's connector configs from a shared control-plane DB —
    another tenant's DSN-bearing rows are never registered into this process.
    """
    log = log or logger
    loaded: list[str] = []
    for config in await repository.list(tenant_id=tenant_id):
        try:
            apply_config(
                registry,
                config.ref,
                config.backend_type,
                config.params,
                secret_provider=secret_provider,
                tenant_id=tenant_id,
                allow_env_fallback=allow_env_fallback,
            )
            loaded.append(config.ref)
        except Exception:
            log.warning(
                "Skipping persisted memory connector %r (backend %r): failed to build",
                config.ref,
                config.backend_type,
                exc_info=True,
            )
    if loaded:
        log.info("Registered persisted memory connectors: %s", ", ".join(loaded))
    return loaded

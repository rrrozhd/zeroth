"""Runtime application of persisted memory connector configurations.

Shared by bootstrap (re-registering persisted connectors at startup) and the
connector management API (registering newly created/updated connectors into
the live registry).
"""

from __future__ import annotations

import logging
from typing import Any

from zeroth.core.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.core.memory.factory import build_connector
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry

logger = logging.getLogger(__name__)


def apply_config(
    registry: InMemoryConnectorRegistry,
    ref: str,
    backend_type: str,
    params: dict[str, Any],
) -> tuple[ConnectorManifest, Any]:
    """Build a connector from a saved config and register it under ``ref``.

    Raises ValueError when the config cannot be built (unknown backend,
    missing params, or missing optional dependency).
    """
    manifest, connector = build_connector(backend_type, params)
    registry.register(ref, manifest, connector)
    return manifest, connector


async def load_persisted_connectors(
    registry: InMemoryConnectorRegistry,
    repository: MemoryConnectorConfigRepository,
    *,
    log: logging.Logger | None = None,
) -> list[str]:
    """Register every persisted connector config into the registry.

    A bad saved config (missing dependency, invalid params) is logged and
    skipped -- it must never break boot. Returns the refs that loaded.
    """
    log = log or logger
    loaded: list[str] = []
    for config in await repository.list():
        try:
            apply_config(registry, config.ref, config.backend_type, config.params)
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

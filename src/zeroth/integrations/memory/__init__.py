"""Memory subsystem for Zeroth agents.

This package provides the building blocks for giving agents persistent memory.
It includes governed-protocol connector implementations, models (data shapes),
and a registry/resolver (looking up and wrapping connectors by name).
"""

from zeroth.integrations.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.integrations.memory.factory import register_memory_connectors
from zeroth.integrations.memory.models import (
    ConnectorManifest,
    ResolvedMemoryBinding,
)
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver

__all__ = [
    "ConnectorManifest",
    "InMemoryConnectorRegistry",
    "KeyValueMemoryConnector",
    "MemoryConnectorResolver",
    "ResolvedMemoryBinding",
    "RunEphemeralMemoryConnector",
    "ThreadMemoryConnector",
    "register_memory_connectors",
]

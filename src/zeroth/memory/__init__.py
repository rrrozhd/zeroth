"""Memory subsystem for Zeroth agents.

This package provides the building blocks for giving agents persistent memory.
It includes GovernAI-protocol connector implementations, models (data shapes),
and a registry/resolver (looking up and wrapping connectors by name).
"""

from zeroth.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.memory.factory import register_memory_connectors
from zeroth.memory.models import (
    ConnectorManifest,
    ResolvedMemoryBinding,
)
from zeroth.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver

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

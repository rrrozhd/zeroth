"""Memory subsystem for Zeroth agents.

This package provides the building blocks for giving agents persistent memory.
It includes connector interfaces (how to read/write), models (data shapes),
and a registry (looking up the right connector by name).
"""

from zeroth.memory.connectors import (
    KeyValueMemoryConnector,
    MemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.memory.factory import register_memory_connectors
from zeroth.memory.models import (
    ConnectorManifest,
    ConnectorScope,
    MemoryContext,
    ResolvedMemoryBinding,
)
from zeroth.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver

__all__ = [
    "ConnectorManifest",
    "ConnectorScope",
    "InMemoryConnectorRegistry",
    "KeyValueMemoryConnector",
    "MemoryConnector",
    "MemoryConnectorResolver",
    "MemoryContext",
    "ResolvedMemoryBinding",
    "RunEphemeralMemoryConnector",
    "ThreadMemoryConnector",
    "register_memory_connectors",
]

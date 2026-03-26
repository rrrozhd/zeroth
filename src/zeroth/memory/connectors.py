"""In-memory connector implementations for agent memory.

Each connector stores key-value data in plain Python dictionaries.
They differ in how they group (or "bucket") the data -- by run, by thread, etc.
These are the MVP implementations; production versions would use a real database.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Protocol

from zeroth.memory.models import MemoryContext


class MemoryConnector(Protocol):
    """Interface that all memory connectors must follow.

    A memory connector knows how to read and write data for an agent.
    Any class that has a `connector_type` string and `read`/`write` methods
    matching these signatures counts as a MemoryConnector -- no need to
    inherit from this class.
    """

    connector_type: str

    def read(self, context: MemoryContext, key: str) -> Any | None:  # pragma: no cover - protocol
        """Look up a value by key, returning None if it doesn't exist."""
        ...

    def write(
        self, context: MemoryContext, key: str, value: Any
    ) -> None:  # pragma: no cover - protocol
        """Store a value under the given key."""
        ...


class _BaseDictConnector:
    """Shared logic for dictionary-backed memory connectors.

    Stores data in nested Python dicts. Each unique "bucket key" (derived
    from the context) gets its own isolated dictionary of key-value pairs.
    Subclasses just need to set `connector_type`.
    """

    connector_type = "memory"

    def __init__(self) -> None:
        # Outer key = bucket name (derived from context), inner dict = actual key-value pairs
        self._state: dict[str, dict[str, Any]] = {}

    def read(self, context: MemoryContext, key: str) -> Any | None:
        """Return the stored value for the key, or None if not found."""
        return self._bucket(context).get(key)

    def write(self, context: MemoryContext, key: str, value: Any) -> None:
        """Save a value under the given key in the appropriate bucket."""
        self._bucket(context)[key] = value

    def _bucket(self, context: MemoryContext) -> MutableMapping[str, Any]:
        """Get (or create) the dictionary for this context's bucket."""
        return self._state.setdefault(self._bucket_key(context), {})

    def _bucket_key(self, context: MemoryContext) -> str:
        """Decide which bucket to use based on the context's instance ID."""
        return context.instance_id


class RunEphemeralMemoryConnector(_BaseDictConnector):
    """Memory that only lasts for a single run and is thrown away afterward.

    Use this when an agent needs scratch space during one execution but
    the data doesn't need to survive after the run finishes.
    """

    connector_type = "ephemeral"


class KeyValueMemoryConnector(_BaseDictConnector):
    """Simple key-value memory connector.

    A general-purpose connector for storing and retrieving data by key.
    """

    connector_type = "key_value"


class ThreadMemoryConnector(_BaseDictConnector):
    """Memory scoped to a conversation thread.

    Use this when data should persist across multiple runs within the
    same thread (like a conversation) but stay separate between threads.
    """

    connector_type = "thread"

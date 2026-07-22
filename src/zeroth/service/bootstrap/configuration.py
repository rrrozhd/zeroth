"""Default configuration objects used by the service bootstrap."""

from __future__ import annotations


class _BootstrapMemorySubsection:
    """Tiny helper providing default attribute values for memory sub-settings."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _BootstrapMemorySettings:
    """Default memory settings used by bootstrap when no ZerothSettings is available.

    Provides the attribute shape expected by ``register_memory_connectors``:
    ``memory``, ``pgvector``, ``chroma``, and ``elasticsearch`` sub-objects.
    All external backends are disabled by default; only in-memory connectors
    are registered.
    """

    def __init__(self) -> None:
        self.memory = _BootstrapMemorySubsection(
            default_connector="ephemeral",
            redis_kv_prefix="zeroth:mem:kv",
            redis_thread_prefix="zeroth:mem:thread",
        )
        self.pgvector = _BootstrapMemorySubsection(
            enabled=False,
            table_name="zeroth_memory_vectors",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        )
        self.chroma = _BootstrapMemorySubsection(
            enabled=False,
            host="localhost",
            port=8000,
            collection_prefix="zeroth_memory",
        )
        self.elasticsearch = _BootstrapMemorySubsection(
            enabled=False,
            hosts=["http://localhost:9200"],
            index_prefix="zeroth_memory",
        )

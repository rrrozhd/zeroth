"""Factory for creating and registering memory connectors based on config.

Reads application settings to decide which connector backends are enabled,
creates singleton connector instances, and registers them in the
InMemoryConnectorRegistry so agents can resolve them by type name string
at runtime.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from zeroth.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.memory.models import ConnectorManifest, ConnectorScope
from zeroth.memory.registry import InMemoryConnectorRegistry

logger = logging.getLogger(__name__)

# Lazy imports for optional backends -- these modules may not be installed
# in all environments.  Using contextlib.suppress keeps things tidy.
RedisKVMemoryConnector: Any = None
RedisThreadMemoryConnector: Any = None
PgvectorMemoryConnector: Any = None
ChromaDBMemoryConnector: Any = None
ElasticsearchMemoryConnector: Any = None
chromadb: Any = None
AsyncElasticsearch: Any = None

with contextlib.suppress(ImportError):
    from zeroth.memory.redis_kv import RedisKVMemoryConnector  # type: ignore[assignment]

with contextlib.suppress(ImportError):
    from zeroth.memory.redis_thread import RedisThreadMemoryConnector  # type: ignore[assignment]

with contextlib.suppress(ImportError):
    from zeroth.memory.pgvector_connector import PgvectorMemoryConnector  # type: ignore[assignment]

with contextlib.suppress(ImportError):
    from zeroth.memory.chroma_connector import ChromaDBMemoryConnector  # type: ignore[assignment]

with contextlib.suppress(ImportError):
    import chromadb  # type: ignore[assignment,no-redef]

with contextlib.suppress(ImportError):
    from zeroth.memory.elastic_connector import (
        ElasticsearchMemoryConnector,  # type: ignore[assignment]
    )

with contextlib.suppress(ImportError):
    from elasticsearch import AsyncElasticsearch  # type: ignore[assignment,no-redef]


def register_memory_connectors(
    registry: InMemoryConnectorRegistry,
    settings: Any,
    *,
    redis_client: Any | None = None,
    pg_conninfo: str | None = None,
) -> None:
    """Create and register all configured memory connectors.

    Always registers the three in-memory connectors (ephemeral, key_value,
    thread) for dev/test. Conditionally registers external backend connectors
    based on settings and available clients.

    Args:
        registry: The connector registry to populate.
        settings: Application settings with memory/pgvector/chroma/elasticsearch config.
        redis_client: An async Redis client instance (if Redis is available).
        pg_conninfo: Postgres connection string (if Postgres is available).
    """
    # Always register in-memory connectors for dev/test
    registry.register(
        "ephemeral",
        ConnectorManifest(connector_type="ephemeral", scope=ConnectorScope.RUN),
        RunEphemeralMemoryConnector(),
    )
    registry.register(
        "key_value",
        ConnectorManifest(connector_type="key_value", scope=ConnectorScope.SHARED),
        KeyValueMemoryConnector(),
    )
    registry.register(
        "thread",
        ConnectorManifest(connector_type="thread", scope=ConnectorScope.THREAD),
        ThreadMemoryConnector(),
    )
    logger.info("Registered in-memory connectors: ephemeral, key_value, thread")

    # Redis connectors (if redis client available)
    if redis_client is not None:
        _register_redis_connectors(registry, settings, redis_client)

    # pgvector (if enabled and pg_conninfo available)
    if settings.pgvector.enabled and pg_conninfo:
        _register_pgvector_connector(registry, settings, pg_conninfo)

    # ChromaDB (if enabled)
    if settings.chroma.enabled:
        _register_chroma_connector(registry, settings)

    # Elasticsearch (if enabled)
    if settings.elasticsearch.enabled:
        _register_elasticsearch_connector(registry, settings)


def _register_redis_connectors(
    registry: InMemoryConnectorRegistry,
    settings: Any,
    redis_client: Any,
) -> None:
    """Register Redis KV and thread memory connectors."""
    if RedisKVMemoryConnector is None or RedisThreadMemoryConnector is None:
        logger.warning("Redis memory connector classes not available, skipping")
        return

    registry.register(
        "redis_kv",
        ConnectorManifest(connector_type="redis_kv", scope=ConnectorScope.SHARED),
        RedisKVMemoryConnector(
            redis_client,
            key_prefix=settings.memory.redis_kv_prefix,
        ),
    )
    registry.register(
        "redis_thread",
        ConnectorManifest(connector_type="redis_thread", scope=ConnectorScope.THREAD),
        RedisThreadMemoryConnector(
            redis_client,
            key_prefix=settings.memory.redis_thread_prefix,
        ),
    )
    logger.info("Registered Redis connectors: redis_kv, redis_thread")


def _register_pgvector_connector(
    registry: InMemoryConnectorRegistry,
    settings: Any,
    pg_conninfo: str,
) -> None:
    """Register pgvector memory connector."""
    if PgvectorMemoryConnector is None:
        logger.warning("PgvectorMemoryConnector not available, skipping")
        return

    registry.register(
        "pgvector",
        ConnectorManifest(connector_type="pgvector", scope=ConnectorScope.SHARED),
        PgvectorMemoryConnector(
            pg_conninfo,
            table_name=settings.pgvector.table_name,
            embedding_model=settings.pgvector.embedding_model,
            embedding_dimensions=settings.pgvector.embedding_dimensions,
        ),
    )
    logger.info("Registered pgvector connector")


def _register_chroma_connector(
    registry: InMemoryConnectorRegistry,
    settings: Any,
) -> None:
    """Register ChromaDB memory connector."""
    if chromadb is None or ChromaDBMemoryConnector is None:
        logger.warning("ChromaDB dependencies not available, skipping")
        return

    client = chromadb.HttpClient(
        host=settings.chroma.host,
        port=settings.chroma.port,
    )

    registry.register(
        "chroma",
        ConnectorManifest(connector_type="chroma", scope=ConnectorScope.SHARED),
        ChromaDBMemoryConnector(
            client,
            collection_prefix=settings.chroma.collection_prefix,
        ),
    )
    logger.info("Registered ChromaDB connector")


def _register_elasticsearch_connector(
    registry: InMemoryConnectorRegistry,
    settings: Any,
) -> None:
    """Register Elasticsearch memory connector."""
    if AsyncElasticsearch is None or ElasticsearchMemoryConnector is None:
        logger.warning("Elasticsearch dependencies not available, skipping")
        return

    client = AsyncElasticsearch(hosts=settings.elasticsearch.hosts)

    registry.register(
        "elasticsearch",
        ConnectorManifest(connector_type="elasticsearch", scope=ConnectorScope.SHARED),
        ElasticsearchMemoryConnector(
            client,
            index_prefix=settings.elasticsearch.index_prefix,
        ),
    )
    logger.info("Registered Elasticsearch connector")

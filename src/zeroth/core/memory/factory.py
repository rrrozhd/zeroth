"""Factory for creating and registering memory connectors based on config.

Reads application settings to decide which connector backends are enabled,
creates singleton connector instances, and registers them in the
InMemoryConnectorRegistry so agents can resolve them by type name string
at runtime.

``build_connector`` is the single construction path shared by the env-based
bootstrap registration and the runtime connector-management API
(POST/PUT /v1/connectors).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from zeroth.core.governed.memory.models import MemoryScope
from zeroth.core.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry

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
    from zeroth.core.memory.redis_kv import RedisKVMemoryConnector  # type: ignore[assignment]

with contextlib.suppress(ImportError):
    from zeroth.core.memory.redis_thread import (
        RedisThreadMemoryConnector,  # type: ignore[assignment]
    )

with contextlib.suppress(ImportError):
    from zeroth.core.memory.pgvector_connector import (
        PgvectorMemoryConnector,  # type: ignore[assignment]
    )

with contextlib.suppress(ImportError):
    from zeroth.core.memory.chroma_connector import (
        ChromaDBMemoryConnector,  # type: ignore[assignment]
    )

with contextlib.suppress(ImportError):
    import chromadb  # type: ignore[assignment,no-redef]

with contextlib.suppress(ImportError):
    from zeroth.core.memory.elastic_connector import (
        ElasticsearchMemoryConnector,  # type: ignore[assignment]
    )

with contextlib.suppress(ImportError):
    from elasticsearch import AsyncElasticsearch  # type: ignore[assignment,no-redef]


#: Backend types accepted by :func:`build_connector`.
SUPPORTED_BACKEND_TYPES: tuple[str, ...] = (
    "pgvector",
    "chroma",
    "elasticsearch",
    "redis_kv",
    "redis_thread",
)


def _require_param(params: dict[str, Any], key: str, backend_type: str) -> Any:
    value = params.get(key)
    if value in (None, "", []):
        raise ValueError(f"{backend_type!r} connector requires a {key!r} parameter")
    return value


def build_connector(
    backend_type: str,
    params: dict[str, Any],
    *,
    redis_client: Any | None = None,
) -> tuple[ConnectorManifest, Any]:
    """Build a memory connector instance from a backend type and params dict.

    Shared by env-based bootstrap registration and the runtime connector
    API. Construction is cheap and does NOT open network connections for
    lazily-connecting backends (pgvector); use the /test endpoint for a
    real connectivity probe.

    Args:
        backend_type: One of ``SUPPORTED_BACKEND_TYPES``.
        params: Backend-specific parameters:
            - ``pgvector``: dsn (required), table_name?, embedding_model?,
              embedding_dimensions?
            - ``chroma``: host (required), port? (default 8000),
              collection_prefix?
            - ``elasticsearch``: hosts (required, list of URLs), index_prefix?
            - ``redis_kv`` / ``redis_thread``: url (required, redis://...),
              key_prefix?
        redis_client: Pre-built async Redis client (env-based bootstrap path
            shares one client). When provided for redis backends, ``url`` is
            not required.

    Returns:
        ``(manifest, connector)`` ready for ``registry.register``.

    Raises:
        ValueError: Unknown backend type, missing/invalid params, or the
            optional dependency for the backend is not installed.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")

    if backend_type == "pgvector":
        return _build_pgvector(params)
    if backend_type == "chroma":
        return _build_chroma(params)
    if backend_type == "elasticsearch":
        return _build_elasticsearch(params)
    if backend_type in ("redis_kv", "redis_thread"):
        return _build_redis(backend_type, params, redis_client=redis_client)
    raise ValueError(
        f"unknown backend_type {backend_type!r}; supported: {', '.join(SUPPORTED_BACKEND_TYPES)}"
    )


def _build_pgvector(params: dict[str, Any]) -> tuple[ConnectorManifest, Any]:
    if PgvectorMemoryConnector is None:
        raise ValueError(
            "pgvector connector dependencies not installed; install zeroth-core[memory-pg]"
        )
    dsn = _require_param(params, "dsn", "pgvector")
    kwargs: dict[str, Any] = {}
    if params.get("table_name"):
        kwargs["table_name"] = params["table_name"]
    if params.get("embedding_model"):
        kwargs["embedding_model"] = params["embedding_model"]
    if params.get("embedding_dimensions"):
        kwargs["embedding_dimensions"] = int(params["embedding_dimensions"])
    # PgvectorMemoryConnector accepts a DSN string and wraps it in an async
    # connection factory internally (connection is lazy -- first use connects).
    connector = PgvectorMemoryConnector(dsn, **kwargs)
    manifest = ConnectorManifest(connector_type="pgvector", scope=MemoryScope.SHARED)
    return manifest, connector


def _build_chroma(params: dict[str, Any]) -> tuple[ConnectorManifest, Any]:
    if chromadb is None or ChromaDBMemoryConnector is None:
        raise ValueError(
            "chroma connector dependencies not installed; install zeroth-core[memory-chroma]"
        )
    host = _require_param(params, "host", "chroma")
    port = int(params.get("port") or 8000)
    client = chromadb.HttpClient(host=host, port=port)
    kwargs: dict[str, Any] = {}
    if params.get("collection_prefix"):
        kwargs["collection_prefix"] = params["collection_prefix"]
    connector = ChromaDBMemoryConnector(client, **kwargs)
    manifest = ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED)
    return manifest, connector


def _build_elasticsearch(params: dict[str, Any]) -> tuple[ConnectorManifest, Any]:
    if AsyncElasticsearch is None or ElasticsearchMemoryConnector is None:
        raise ValueError(
            "elasticsearch connector dependencies not installed; install zeroth-core[memory-es]"
        )
    hosts = _require_param(params, "hosts", "elasticsearch")
    if isinstance(hosts, str):
        hosts = [hosts]
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError("'hosts' must be a list of URL strings")
    client = AsyncElasticsearch(hosts=hosts)
    kwargs: dict[str, Any] = {}
    if params.get("index_prefix"):
        kwargs["index_prefix"] = params["index_prefix"]
    connector = ElasticsearchMemoryConnector(client, **kwargs)
    manifest = ConnectorManifest(connector_type="elasticsearch", scope=MemoryScope.SHARED)
    return manifest, connector


def _build_redis(
    backend_type: str,
    params: dict[str, Any],
    *,
    redis_client: Any | None = None,
) -> tuple[ConnectorManifest, Any]:
    connector_cls = (
        RedisKVMemoryConnector if backend_type == "redis_kv" else RedisThreadMemoryConnector
    )
    if connector_cls is None:
        raise ValueError(
            f"{backend_type} connector dependencies not installed; "
            "install zeroth-core[dispatch] (redis)"
        )
    client = redis_client
    if client is None:
        url = _require_param(params, "url", backend_type)
        if not isinstance(url, str) or not url.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("'url' must be a redis:// / rediss:// / unix:// URL")
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - redis is a core-ish dep
            raise ValueError(
                "redis client library not installed; install zeroth-core[dispatch]"
            ) from exc
        client = aioredis.from_url(url)
    kwargs: dict[str, Any] = {}
    if params.get("key_prefix"):
        kwargs["key_prefix"] = params["key_prefix"]
    connector = connector_cls(client, **kwargs)
    scope = MemoryScope.SHARED if backend_type == "redis_kv" else MemoryScope.THREAD
    manifest = ConnectorManifest(connector_type=backend_type, scope=scope)
    return manifest, connector


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
        ConnectorManifest(connector_type="ephemeral", scope=MemoryScope.RUN),
        RunEphemeralMemoryConnector(),
    )
    registry.register(
        "key_value",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.SHARED),
        KeyValueMemoryConnector(),
    )
    registry.register(
        "thread",
        ConnectorManifest(connector_type="thread", scope=MemoryScope.THREAD),
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

    for ref, prefix in (
        ("redis_kv", settings.memory.redis_kv_prefix),
        ("redis_thread", settings.memory.redis_thread_prefix),
    ):
        manifest, connector = build_connector(
            ref, {"key_prefix": prefix}, redis_client=redis_client
        )
        registry.register(ref, manifest, connector)
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

    manifest, connector = build_connector(
        "pgvector",
        {
            "dsn": pg_conninfo,
            "table_name": settings.pgvector.table_name,
            "embedding_model": settings.pgvector.embedding_model,
            "embedding_dimensions": settings.pgvector.embedding_dimensions,
        },
    )
    registry.register("pgvector", manifest, connector)
    logger.info("Registered pgvector connector")


def _register_chroma_connector(
    registry: InMemoryConnectorRegistry,
    settings: Any,
) -> None:
    """Register ChromaDB memory connector."""
    if chromadb is None or ChromaDBMemoryConnector is None:
        logger.warning("ChromaDB dependencies not available, skipping")
        return

    manifest, connector = build_connector(
        "chroma",
        {
            "host": settings.chroma.host,
            "port": settings.chroma.port,
            "collection_prefix": settings.chroma.collection_prefix,
        },
    )
    registry.register("chroma", manifest, connector)
    logger.info("Registered ChromaDB connector")


def _register_elasticsearch_connector(
    registry: InMemoryConnectorRegistry,
    settings: Any,
) -> None:
    """Register Elasticsearch memory connector."""
    if AsyncElasticsearch is None or ElasticsearchMemoryConnector is None:
        logger.warning("Elasticsearch dependencies not available, skipping")
        return

    manifest, connector = build_connector(
        "elasticsearch",
        {
            "hosts": list(settings.elasticsearch.hosts),
            "index_prefix": settings.elasticsearch.index_prefix,
        },
    )
    registry.register("elasticsearch", manifest, connector)
    logger.info("Registered Elasticsearch connector")

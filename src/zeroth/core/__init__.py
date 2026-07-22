"""Zeroth core package exports.

This package re-exports the most common storage primitives so callers can
import them from ``zeroth.core`` without reaching into subpackages.

``AsyncPostgresDatabase`` is gated behind the ``[memory-pg]`` extra and is
imported lazily via ``__getattr__`` so that a base ``pip install zeroth-core``
can import ``zeroth.core`` without requiring ``psycopg``.
"""

from typing import TYPE_CHECKING, Any

from zeroth.platform.storage import (
    AsyncConnection,
    AsyncDatabase,
    AsyncSQLiteDatabase,
    Migration,
    RedisConfig,
    RedisDeploymentMode,
    SQLiteDatabase,
    create_database,
    docker_container_running,
)

if TYPE_CHECKING:
    from zeroth.integrations.persistence.governed_redis import (
        GovernAIRedisRuntimeStores,
        build_governai_redis_runtime,
    )
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

__all__ = [
    "AsyncConnection",
    "AsyncDatabase",
    "AsyncPostgresDatabase",
    "AsyncSQLiteDatabase",
    "GovernAIRedisRuntimeStores",
    "Migration",
    "RedisConfig",
    "RedisDeploymentMode",
    "SQLiteDatabase",
    "build_governai_redis_runtime",
    "create_database",
    "docker_container_running",
]


def __getattr__(name: str) -> Any:
    """Lazily import Postgres-backed and governed-runtime symbols.

    ``AsyncPostgresDatabase`` requires the ``[memory-pg]`` extra. The governed
    store factory lives in the integrations layer, so resolving it eagerly
    would put runtime and governance code on every ``zeroth.core`` import.
    """
    if name == "AsyncPostgresDatabase":
        from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

        return AsyncPostgresDatabase
    if name in {"GovernAIRedisRuntimeStores", "build_governai_redis_runtime"}:
        from zeroth.integrations.persistence import governed_redis

        return getattr(governed_redis, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

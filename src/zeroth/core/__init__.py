"""Zeroth core package exports.

This package re-exports the most common storage primitives so callers can
import them from ``zeroth.core`` without reaching into subpackages.

``AsyncPostgresDatabase`` is gated behind the ``[memory-pg]`` extra and is
imported lazily via ``__getattr__`` so that a base ``pip install zeroth-core``
can import ``zeroth.core`` without requiring ``psycopg``.
"""

from typing import TYPE_CHECKING, Any

from zeroth.core.storage import (
    AsyncConnection,
    AsyncDatabase,
    AsyncSQLiteDatabase,
    GovernAIRedisRuntimeStores,
    Migration,
    RedisConfig,
    RedisDeploymentMode,
    SQLiteDatabase,
    build_governai_redis_runtime,
    create_database,
    docker_container_running,
)

if TYPE_CHECKING:
    from zeroth.core.storage.async_postgres import AsyncPostgresDatabase

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
    """Lazily import Postgres-backed symbols (require [memory-pg] extra)."""
    if name == "AsyncPostgresDatabase":
        from zeroth.core.storage.async_postgres import AsyncPostgresDatabase

        return AsyncPostgresDatabase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

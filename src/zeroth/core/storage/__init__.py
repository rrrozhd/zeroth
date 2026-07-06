"""Storage primitives shared by Zeroth subsystems.

This package provides the database and caching backends that other parts
of Zeroth use: SQLite for local persistence, Redis for distributed runtime
state, and JSON helpers for serialization.

Postgres support (``AsyncPostgresDatabase``) is gated behind the ``[memory-pg]``
extra and imported lazily so that a base ``pip install zeroth-core`` does not
require ``psycopg`` / ``psycopg-pool`` at import time.
"""

from typing import TYPE_CHECKING, Any

from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.core.storage.database import AsyncConnection, AsyncDatabase
from zeroth.core.storage.factory import create_database
from zeroth.core.storage.redis import (
    GovernAIRedisRuntimeStores,
    RedisConfig,
    RedisDeploymentMode,
    build_governai_redis_runtime,
    docker_container_running,
)
from zeroth.core.storage.sqlite import EncryptedField, Migration, SQLiteDatabase

if TYPE_CHECKING:
    from zeroth.core.storage.async_postgres import AsyncPostgresDatabase

__all__ = [
    "AsyncConnection",
    "AsyncDatabase",
    "AsyncPostgresDatabase",
    "AsyncSQLiteDatabase",
    "EncryptedField",
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

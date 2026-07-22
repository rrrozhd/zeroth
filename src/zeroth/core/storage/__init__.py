"""Legacy import path for the platform storage package.

The storage primitives live in :mod:`zeroth.platform.storage`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).

``AsyncPostgresDatabase`` stays lazy (requires the ``[memory-pg]`` extra), and
the governed-runtime store factory stays lazy because it now lives in the
integrations layer (:mod:`zeroth.integrations.persistence.governed_redis`);
resolving it eagerly would put runtime and governance code on the import path
of everything that touches storage.
"""

from typing import Any

from zeroth.platform.storage import (
    AsyncConnection,
    AsyncDatabase,
    AsyncSQLiteDatabase,
    EncryptedField,
    Migration,
    RedisConfig,
    RedisDeploymentMode,
    SQLiteDatabase,
    create_database,
    docker_container_running,
    ensure_and_lock_row,
)

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
    "ensure_and_lock_row",
]


def __getattr__(name: str) -> Any:
    """Lazily republish the Postgres database and the governed store factory."""
    if name == "AsyncPostgresDatabase":
        from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

        return AsyncPostgresDatabase
    if name in {"GovernAIRedisRuntimeStores", "build_governai_redis_runtime"}:
        from zeroth.core.storage import redis

        return getattr(redis, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Storage primitives shared by Zeroth subsystems.

This package provides the database and caching backends that other parts
of Zeroth use: SQLite for local persistence, Redis connection configuration,
and JSON helpers for serialization.

Postgres support (``AsyncPostgresDatabase``) is gated behind the ``[memory-pg]``
extra and imported lazily so that a base ``pip install zeroth-core`` does not
require ``psycopg`` / ``psycopg-pool`` at import time.

The governed-runtime store factory that used to live beside ``RedisConfig``
is domain-aware wiring and moved to
:mod:`zeroth.integrations.persistence.governed_redis`; the legacy
``zeroth.core.storage`` paths keep republishing it.
"""

from typing import TYPE_CHECKING, Any

from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage.coordination import ensure_and_lock_row
from zeroth.platform.storage.database import AsyncConnection, AsyncDatabase
from zeroth.platform.storage.factory import create_database
from zeroth.platform.storage.redis import (
    RedisConfig,
    RedisDeploymentMode,
    docker_container_running,
)
from zeroth.platform.storage.sqlite import EncryptedField, Migration, SQLiteDatabase

if TYPE_CHECKING:
    from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

__all__ = [
    "AsyncConnection",
    "AsyncDatabase",
    "AsyncPostgresDatabase",
    "AsyncSQLiteDatabase",
    "EncryptedField",
    "Migration",
    "RedisConfig",
    "RedisDeploymentMode",
    "SQLiteDatabase",
    "create_database",
    "docker_container_running",
    "ensure_and_lock_row",
]


def __getattr__(name: str) -> Any:
    """Lazily import Postgres-backed symbols (require [memory-pg] extra)."""
    if name == "AsyncPostgresDatabase":
        from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

        return AsyncPostgresDatabase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

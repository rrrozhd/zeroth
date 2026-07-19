"""Database factory that selects the backend based on configuration.

Returns an AsyncDatabase instance (either SQLite or Postgres) based
on the ZerothSettings.database.backend value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.platform.storage.database import AsyncDatabase

if TYPE_CHECKING:
    # Annotation-only. An eager import would re-enter the config package while
    # zeroth.platform.config.settings is still initializing: settings imports
    # the artifact-store section, whose parent package init loads the storage
    # closure, which lands back here. The warm-cache suite cannot see that
    # cycle; tests/platform/test_config_surface.py probes it from subprocesses.
    from zeroth.platform.config.settings import ZerothSettings


async def create_database(settings: ZerothSettings) -> AsyncDatabase:
    """Create and return the appropriate async database backend.

    Reads ``settings.database.backend`` to decide:
    - ``"postgres"`` -> AsyncPostgresDatabase with connection pool
    - anything else  -> AsyncSQLiteDatabase (default)
    """
    if settings.database.backend == "postgres":
        from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase

        dsn = settings.database.postgres_dsn.get_secret_value()
        return await AsyncPostgresDatabase.create(
            dsn,
            min_size=settings.database.postgres_pool_min,
            max_size=settings.database.postgres_pool_max,
        )
    else:
        from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase

        return AsyncSQLiteDatabase(
            path=settings.database.sqlite_path,
            encryption_key=(
                settings.database.encryption_key.get_secret_value()
                if settings.database.encryption_key
                else None
            ),
        )

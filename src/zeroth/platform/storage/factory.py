"""Database factory that selects the backend based on configuration.

Returns an AsyncDatabase instance (either SQLite or Postgres) based
on the ZerothSettings.database.backend value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.platform.storage.database import AsyncDatabase

if TYPE_CHECKING:
    # Annotation-only, so importing storage does not execute the config
    # package. While the artifact-store settings section still lived under
    # zeroth.runtime, an eager import here closed a partial-initialization cycle
    # (settings -> artifacts -> zeroth.runtime -> storage -> factory); keeping it
    # lazy also keeps the storage closure minimal.
    # tests/platform/test_config_surface.py probes both cold-import directions.
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

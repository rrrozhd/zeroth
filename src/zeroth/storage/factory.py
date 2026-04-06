"""Database factory that selects the backend based on configuration.

Returns an AsyncDatabase instance (either SQLite or Postgres) based
on the ZerothSettings.database.backend value.
"""

from __future__ import annotations

from zeroth.config.settings import ZerothSettings
from zeroth.storage.database import AsyncDatabase


async def create_database(settings: ZerothSettings) -> AsyncDatabase:
    """Create and return the appropriate async database backend.

    Reads ``settings.database.backend`` to decide:
    - ``"postgres"`` -> AsyncPostgresDatabase with connection pool
    - anything else  -> AsyncSQLiteDatabase (default)
    """
    if settings.database.backend == "postgres":
        from zeroth.storage.async_postgres import AsyncPostgresDatabase

        dsn = settings.database.postgres_dsn.get_secret_value()
        return await AsyncPostgresDatabase.create(
            dsn,
            min_size=settings.database.postgres_pool_min,
            max_size=settings.database.postgres_pool_max,
        )
    else:
        from zeroth.storage.async_sqlite import AsyncSQLiteDatabase

        return AsyncSQLiteDatabase(
            path=settings.database.sqlite_path,
            encryption_key=(
                settings.database.encryption_key.get_secret_value()
                if settings.database.encryption_key
                else None
            ),
        )

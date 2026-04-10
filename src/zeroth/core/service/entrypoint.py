"""Production entrypoint for Zeroth inside Docker.

Runs Alembic migrations (if Postgres backend), bootstraps the service,
and starts uvicorn.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn


def main() -> None:
    """Run migrations and start the Zeroth platform."""
    from zeroth.core.config.settings import get_settings

    settings = get_settings()

    # Run Alembic migrations for Postgres backend
    if settings.database.backend == "postgres" and settings.database.postgres_dsn:
        from zeroth.core.service.bootstrap import run_migrations

        dsn = settings.database.postgres_dsn.get_secret_value()
        print("Running Alembic migrations against Postgres...", flush=True)
        run_migrations(dsn)
        print("Migrations complete.", flush=True)

    # Determine TLS settings
    ssl_certfile = settings.tls.certfile if settings.tls.certfile else None
    ssl_keyfile = settings.tls.keyfile if settings.tls.keyfile else None

    uvicorn.run(
        "zeroth.core.service.entrypoint:app_factory",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        factory=True,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )


async def _bootstrap():
    """Create database, bootstrap service, and return the FastAPI app."""
    from zeroth.core.config.settings import get_settings
    from zeroth.core.service.app import create_app
    from zeroth.core.service.bootstrap import bootstrap_service
    from zeroth.core.storage.factory import create_database

    settings = get_settings()
    database = await create_database(settings)

    deployment_ref = os.environ.get("ZEROTH_DEPLOYMENT_REF", "default")
    bootstrap = await bootstrap_service(database, deployment_ref=deployment_ref)
    return create_app(bootstrap)


def app_factory():
    """Uvicorn factory function -- returns a fully wired FastAPI app."""
    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    main()

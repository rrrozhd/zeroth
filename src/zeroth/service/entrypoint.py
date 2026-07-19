"""Production entrypoint for the Zeroth service.

Runs Alembic migrations (SQLite or Postgres), bootstraps the service,
and starts uvicorn. Invoked by `zeroth-core serve` or directly via
`python -m zeroth.core.service.entrypoint`.
"""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

logger = logging.getLogger(__name__)


def main() -> None:
    """Run migrations and start the Zeroth platform."""
    from zeroth.core.cli import ensure_schema
    from zeroth.core.config.settings import get_settings

    settings = get_settings()

    logging.basicConfig(level=logging.INFO)
    logger.info("Running Alembic migrations (%s backend)...", settings.database.backend)
    ensure_schema()
    logger.info("Migrations complete.")

    asyncio.run(_serve(settings))


async def _serve(settings) -> None:
    """Bootstrap the app and serve it on a single event loop.

    Bootstrap and uvicorn must share one loop: bootstrap starts background
    tasks (durable run worker, SLA checker) that would otherwise bind to a
    dead loop. This is also why uvicorn's string-import `factory=True` mode
    cannot be used here — it calls the factory from inside its own running
    loop, where a nested `asyncio.run()` raises RuntimeError.
    """
    app = await _bootstrap()

    ssl_certfile = settings.tls.certfile if settings.tls.certfile else None
    ssl_keyfile = settings.tls.keyfile if settings.tls.keyfile else None

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )
    await uvicorn.Server(config).serve()


async def _bootstrap():
    """Create database, bootstrap service, and return the FastAPI app."""
    from zeroth.core.config.settings import get_settings
    from zeroth.core.storage.factory import create_database
    from zeroth.service.app import create_app
    from zeroth.service.bootstrap.factory import bootstrap_service

    settings = get_settings()
    database = await create_database(settings)

    deployment_ref = os.environ.get("ZEROTH_DEPLOYMENT_REF", "default")

    # WS-F: one process-wide secret provider, reused for LLM keys, HTTP-client
    # auth, the WS-D signing key, and execution-unit env resolution. Built here
    # (before runners) so the runners resolve their keys through it; the same
    # instance is handed to bootstrap_service so nothing builds a second one.
    from zeroth.core.secrets import build_secret_provider

    secret_provider = build_secret_provider(settings.secrets)
    warm = getattr(secret_provider, "warm", None)
    if callable(warm):
        await warm()

    # Build agent runners from the deployment's own graph definition so
    # declaratively-authored graphs (studio canvas, seed-demo) execute on
    # the stock entrypoint. Opt out with ZEROTH_AUTO_AGENT_RUNNERS=false
    # (e.g. when a custom entrypoint supplies hand-wired runners).
    agent_runners = None
    auto_runners = os.environ.get("ZEROTH_AUTO_AGENT_RUNNERS", "true").lower()
    if auto_runners not in {"0", "false", "no"}:
        from zeroth.core.agent_runtime.factory import build_runners_for_deployment

        agent_runners = await build_runners_for_deployment(
            database,
            deployment_ref,
            secret_provider=secret_provider,
            allow_env_fallback=settings.secrets.allow_env_fallback,
            llm_key_map=settings.secrets.llm_key_map,
        )
        if agent_runners:
            logger.info(
                "auto-built %d agent runner(s) from deployment %r: %s",
                len(agent_runners),
                deployment_ref,
                sorted(agent_runners),
            )

    bootstrap = await bootstrap_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners=agent_runners,
        secret_provider=secret_provider,
    )
    return create_app(bootstrap)


def app_factory():
    """Uvicorn factory function -- returns a fully wired FastAPI app."""
    return asyncio.run(_bootstrap())


if __name__ == "__main__":
    main()

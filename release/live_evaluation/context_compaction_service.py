"""Run the provider-free context checkpoint as a real local Zeroth service."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path


async def build_context_checkpoint_app(database_path: Path, api_key: str):
    """Build a deployment-bound app whose provider boundary is local-only."""
    # Imports that construct the embedded economics plane are deliberately
    # lazy: ``main`` first pins every writable subsystem below --external-root.
    from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
    from zeroth.service.api.authentication import ServiceAuthConfig, StaticApiKeyCredential
    from zeroth.service.app import create_app
    from zeroth.service.bootstrap.factory import (
        bootstrap_scoped_service,
        build_runners_for_deployment,
    )

    from .context_compaction_checkpoint import (
        CONTEXT_DEPLOYMENT_REF,
        CONTEXT_TENANT_ID,
        ProviderFreeContextAdapter,
        seed_context_fixture,
    )

    database = AsyncSQLiteDatabase(path=str(database_path))
    await seed_context_fixture(database)
    provider = ProviderFreeContextAdapter()
    runners = await build_runners_for_deployment(
        database,
        CONTEXT_DEPLOYMENT_REF,
        tenant_id=CONTEXT_TENANT_ID,
        provider=provider,
    )
    auth = ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="context-checkpoint",
                secret=api_key,
                subject="context-checkpoint-operator",
                roles=["admin"],
                tenant_id=CONTEXT_TENANT_ID,
            )
        ]
    )
    bootstrap = await bootstrap_scoped_service(
        database,
        deployment_ref=CONTEXT_DEPLOYMENT_REF,
        tenant_id=CONTEXT_TENANT_ID,
        agent_runners=runners,
        auth_config=auth,
        enable_durable_worker=True,
    )
    app = create_app(bootstrap)
    app.state.context_checkpoint_provider = provider
    app.state.context_checkpoint_database = database
    return app


async def _serve(database_path: Path, api_key: str, host: str, port: int) -> None:
    import uvicorn

    app = await build_context_checkpoint_app(database_path, api_key)
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port, proxy_headers=True)).serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--external-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8124)
    args = parser.parse_args(argv)
    api_key = os.environ.get("ZEROTH_CONTEXT_CHECKPOINT_API_KEY")
    if not api_key:
        parser.error("ZEROTH_CONTEXT_CHECKPOINT_API_KEY is required")
    root = args.external_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifacts = root / "artifacts"
    connector_spool = root / "connector-spool"
    artifacts.mkdir(parents=True, exist_ok=True)
    connector_spool.mkdir(parents=True, exist_ok=True)
    os.environ["ZEROTH_DATABASE__BACKEND"] = "sqlite"
    os.environ["ZEROTH_DATABASE__SQLITE_PATH"] = str(args.database)
    os.environ["ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR"] = str(artifacts)
    os.environ["ECP_DATABASE_URL"] = f"sqlite+pysqlite:///{root / 'econ-plane.sqlite3'}"
    os.environ["ECP_CONNECTOR_SPOOL_ROOT"] = str(connector_spool)
    from zeroth.service.cli import ensure_schema

    ensure_schema()
    asyncio.run(_serve(args.database, api_key, args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

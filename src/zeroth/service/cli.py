"""The `zeroth-core` command-line interface.

Subcommands:

- ``zeroth-core serve``      — run migrations (SQLite or Postgres) and start
  the service on the deployment named by ``ZEROTH_DEPLOYMENT_REF``.
- ``zeroth-core seed-demo``  — create the schema and seed a runnable demo
  deployment (contracts + published single-agent graph) so a fresh install
  can serve its first run without writing Python.
- ``zeroth-core migrate``    — apply database migrations and exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path


def ensure_schema() -> None:
    """Run Alembic migrations for the configured backend (SQLite or Postgres)."""
    from zeroth.platform.config.settings import get_settings
    from zeroth.service.bootstrap import run_migrations

    settings = get_settings()
    if settings.database.backend == "postgres" and settings.database.postgres_dsn:
        run_migrations(settings.database.postgres_dsn.get_secret_value())
    else:
        db_path = Path(settings.database.sqlite_path)
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        run_migrations(f"sqlite:///{db_path}")


def _cmd_migrate(_args: argparse.Namespace) -> int:
    ensure_schema()
    print("migrations applied")
    return 0


def _cmd_seed_demo(args: argparse.Namespace) -> int:
    async def _run() -> None:
        from zeroth.platform.config.settings import get_settings
        from zeroth.platform.storage.factory import create_database
        from zeroth.service.demo import seed_demo

        ensure_schema()
        database = await create_database(get_settings())
        try:
            deployment = await seed_demo(
                database, deployment_ref=args.deployment_ref, model=args.model
            )
        finally:
            close = getattr(database, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

        print(f"seeded deployment {deployment.deployment_ref!r} ({deployment.graph_version_ref})")
        if not os.environ.get("ZEROTH_SERVICE_API_KEYS_JSON"):
            demo_key = secrets.token_urlsafe(24)
            keys_json = json.dumps(
                [
                    {
                        "credential_id": "demo",
                        "secret": demo_key,
                        "subject": "demo-operator",
                        "roles": ["operator"],
                    }
                ]
            )
            print("\nNo API keys configured. To serve the demo:\n")
            print(f"  export ZEROTH_SERVICE_API_KEYS_JSON='{keys_json}'")
            print(f"  zeroth-core serve --deployment-ref {args.deployment_ref}")
            print("\nThen call it (agent runs need e.g. OPENAI_API_KEY set):\n")
            print(
                f'  curl -s -X POST http://localhost:8000/v1/runs -H "X-API-Key: {demo_key}" '
                '-H "Content-Type: application/json" '
                '-d \'{"input_payload": {"question": "What is Zeroth?"}}\''
            )

    asyncio.run(_run())
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.deployment_ref:
        os.environ["ZEROTH_DEPLOYMENT_REF"] = args.deployment_ref
    if args.port:
        os.environ["PORT"] = str(args.port)

    from zeroth.service.entrypoint import main as entrypoint_main

    entrypoint_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zeroth-core", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run migrations and start the service")
    serve.add_argument("--port", type=int, default=None, help="listen port (default 8000)")
    serve.add_argument(
        "--deployment-ref",
        default=None,
        help="deployment to serve (default: $ZEROTH_DEPLOYMENT_REF or 'default')",
    )
    serve.set_defaults(func=_cmd_serve)

    seed = sub.add_parser("seed-demo", help="seed a runnable demo deployment")
    seed.add_argument("--deployment-ref", default="default")
    seed.add_argument(
        "--model",
        default="openai/gpt-4o-mini",
        help="LiteLLM model id for the demo agent",
    )
    seed.set_defaults(func=_cmd_seed_demo)

    migrate = sub.add_parser("migrate", help="apply database migrations and exit")
    migrate.set_defaults(func=_cmd_migrate)

    from zeroth.check.cli import attach_check_parser

    attach_check_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

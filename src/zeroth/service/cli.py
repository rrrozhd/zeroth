"""The `zeroth-core` command-line interface.

Subcommands:

- ``zeroth-core serve``      — run migrations (SQLite or Postgres) and start
  the service on the deployment named by ``ZEROTH_DEPLOYMENT_REF``.
- ``zeroth-core seed-demo``  — create the schema and seed a runnable demo
  deployment (contracts + published single-agent graph) so a fresh install
  can serve its first run without writing Python.
- ``zeroth-core migrate``    — apply database migrations and exit.
- ``zeroth-core mcp-import`` — pin a registered MCP server's tools into a
  draft graph as ``mcp_tool`` nodes, so an MCP tool has a contract at publish
  time instead of only at run time.
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



def _cmd_mcp_import(args: argparse.Namespace) -> int:
    """Pin a registered server's tools, reporting failures as messages.

    This is frequently the first command an operator runs after installing, so
    it meets the environment before anything else does: an unmigrated database,
    a server command that is not on PATH, one that is not an MCP server at all.
    Every one of those is an answer the operator can act on, and none of them is
    a traceback -- so the schema is ensured first (as ``serve`` and ``seed-demo``
    already do) and the failure paths end in an exit code.
    """
    from zeroth.service.mcp_import import MCPImportError

    async def _run() -> list:
        from zeroth.contracts.graph import GraphRepository
        from zeroth.platform.config.settings import get_settings
        from zeroth.platform.storage.factory import create_database
        from zeroth.service.mcp_import import import_mcp_tools

        database = await create_database(get_settings())
        try:
            return await import_mcp_tools(
                database,
                GraphRepository(database),
                server_ref=args.server,
                graph_id=args.graph,
                agent_node_id=args.agent,
                tool_names=args.tool or None,
                tenant_id=args.tenant,
            )
        finally:
            close = getattr(database, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    try:
        # Without this an unmigrated database answers the registry lookup with a
        # raw sqlite "no such table", which reads as a bug in Zeroth rather than
        # as "run the migrations".
        ensure_schema()
        imported = asyncio.run(_run())
    except MCPImportError as exc:
        print(f"mcp-import failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a CLI answers with a message, not a traceback
        print(f"mcp-import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for tool in imported:
        verb = "re-pinned" if tool.replaced else "pinned"
        print(f"{verb} {tool.tool_name} as {tool.node_id} ({tool.schema_hash[:12]}...)")
        if tool.declaration_flags:
            # Flagged, not blocked: the injection heuristics are conservative,
            # so the declaration is provenance-wrapped on its way to the model
            # rather than withheld. The operator is the one who can judge it.
            print(
                f"  ! {tool.tool_name}'s declared text matches injection heuristics "
                f"({', '.join(tool.declaration_flags)}); it is provenance-wrapped before "
                "the model sees it, not blocked",
                file=sys.stderr,
            )
    print(f"{len(imported)} tool(s) imported into {args.graph!r}; publish to enforce the pins")
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

    mcp_import = sub.add_parser(
        "mcp-import",
        help="pin a registered MCP server's tools into a draft graph",
        description=(
            "Freeze an MCP server's tools as mcp_tool nodes attached to an agent. The "
            "server's command/args/env come from the operator-owned registry; the graph "
            "carries only its ref."
        ),
    )
    mcp_import.add_argument("--server", required=True, help="registered MCP server ref")
    mcp_import.add_argument("--graph", required=True, help="draft graph id to import into")
    mcp_import.add_argument("--agent", required=True, help="agent node the tools attach to")
    mcp_import.add_argument(
        "--tool",
        action="append",
        default=[],
        help="tool to import; repeatable. Omit to import every tool the server offers.",
    )
    mcp_import.add_argument(
        "--tenant", default="default", help="owning tenant (default: %(default)s)"
    )
    mcp_import.set_defaults(func=_cmd_mcp_import)

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

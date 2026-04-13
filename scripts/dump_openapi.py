"""Dump the zeroth-core OpenAPI spec to JSON for offline consumption.

Used by Phase 29 (zeroth-studio repo split) and Phase 32 (OpenAPI consumers)
to produce a reproducible snapshot of the FastAPI OpenAPI document without
requiring a running uvicorn process.

Usage:
    uv run python scripts/dump_openapi.py --out openapi/zeroth-core-openapi.json
    uv run python scripts/dump_openapi.py  # writes to stdout
    uv run python scripts/dump_openapi.py --check --out openapi/zeroth-core-openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump the zeroth-core FastAPI OpenAPI spec to JSON.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file (default: stdout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit 1 if --out file is missing or differs from freshly generated "
            "spec (drift check for CI)."
        ),
    )
    args = parser.parse_args()

    # Import after argparse so --help is fast and free of heavy side effects.
    #
    # We construct a FastAPI app directly via ``create_app`` with a minimal
    # stub bootstrap. ``create_app`` only reads ``bootstrap.authenticator`` and
    # friends at request-time (middleware), so a SimpleNamespace with ``None``
    # fields is sufficient for ``app.openapi()`` — which is a static schema
    # walk of the registered routes. This avoids needing a migrated database,
    # secrets, or any runtime wiring.
    from types import SimpleNamespace

    from zeroth.core.service.app import create_app

    stub_bootstrap = SimpleNamespace(
        deployment=None,
        graph=None,
        contract_registry=None,
        approval_service=None,
        run_repository=None,
        orchestrator=None,
        audit_repository=None,
        authenticator=None,
        regulus_client=None,
        artifact_store=None,       # Phase 40
        template_registry=None,    # Phase 40
    )
    app = create_app(stub_bootstrap)  # type: ignore[arg-type]
    spec = app.openapi()
    text = json.dumps(spec, indent=2, sort_keys=True) + "\n"

    if args.check:
        if args.out is None:
            sys.stderr.write("--check requires --out\n")
            return 1
        if not args.out.exists():
            sys.stderr.write(f"DRIFT: {args.out} does not exist\n")
            return 1
        existing = args.out.read_text()
        if existing != text:
            sys.stderr.write(
                f"DRIFT: {args.out} is stale. Run "
                f"`python scripts/dump_openapi.py --out {args.out}` to update.\n"
            )
            return 1
        sys.stdout.write(f"OK: {args.out} is up to date.\n")
        return 0

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

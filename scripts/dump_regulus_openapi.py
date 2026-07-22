"""Dump the bundled Regulus OpenAPI document deterministically."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump the bundled Regulus OpenAPI spec.")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    from zeroth.econ.plane.main import app

    rendered = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if args.out is None:
            sys.stderr.write("--check requires --out\n")
            return 1
        if not args.out.exists() or args.out.read_text() != rendered:
            sys.stderr.write(f"DRIFT: {args.out} is stale\n")
            return 1
        sys.stdout.write(f"OK: {args.out} is up to date.\n")
        return 0
    if args.out is None:
        sys.stdout.write(rendered)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

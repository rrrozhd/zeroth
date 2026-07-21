"""Generate the console version module from the canonical project metadata."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def _render(pyproject: Path) -> str:
    with pyproject.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    return (
        "// Generated from pyproject.toml; run `npm run gen:version`.\n"
        f'export const VERSION = "{version}";\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    expected = _render(args.pyproject)
    if args.check:
        actual = args.out.read_text() if args.out.exists() else None
        if actual != expected:
            print(f"DRIFT: {args.out} is not generated from {args.pyproject}", file=sys.stderr)
            return 1
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

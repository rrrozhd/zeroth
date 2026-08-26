"""Run one candidate migration without owning any certification result channel."""

from __future__ import annotations

import argparse
import importlib
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any


def _load_target(reference: str) -> Any:
    module_name, _, attribute_path = reference.partition(":")
    value: Any = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        value = getattr(value, attribute)
    return value


def _run_migration(root: Path, reference: str, database_url: str) -> None:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        sys.path.insert(0, str(root))
        try:
            runner = _load_target(reference)
            if not callable(runner):
                raise ValueError("migration_runner target must be callable")
            runner(database_url)
        finally:
            sys.path.pop(0)


def main(argv: list[str] | None = None) -> int:
    """Execute a migration; the trusted parent verifies only its database effect."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_process")
    parser.add_argument("name", choices=("run-migration",))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    try:
        _run_migration(args.root.resolve(), args.reference, args.database_url)
    except Exception as error:  # noqa: BLE001 - untrusted faults become diagnostics
        print(f"run-migration: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build candidate-bound load/recovery evidence from retained raw observations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.load.report import build_report, load_baseline, load_profiles  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--profiles", type=Path, required=True)
    run.add_argument("--baseline", type=Path, required=True)
    run.add_argument("--identity", type=Path, required=True)
    run.add_argument("--observations", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    return parser


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            load_profiles(args.profiles),
            load_baseline(args.baseline),
            _read(args.identity),
            _read(args.observations),
        )
    except (OSError, TypeError, ValueError) as error:
        report = {"schema_version": 1, "passed": False, "errors": [str(error)]}
    _write(args.output, report)
    if not report["passed"]:
        print("\n".join(report["errors"] or ["load/recovery threshold failed"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

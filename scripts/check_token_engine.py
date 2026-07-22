#!/usr/bin/env python3
"""Run the finite-domain token-engine model checker."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from token_engine_checker.runner import run_check


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, choices=(4, 5, 6), required=True)
    parser.add_argument("--exhaustive", action="store_true")
    parser.add_argument("--cases", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report_path = arguments.report or Path(f"token-engine-report-n{arguments.nodes}.json")
    try:
        report = run_check(
            nodes=arguments.nodes,
            exhaustive=arguments.exhaustive,
            cases=arguments.cases,
            seed=arguments.seed,
            report_path=report_path,
        )
    except ValueError as error:
        _parser().error(str(error))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())

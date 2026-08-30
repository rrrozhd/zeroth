"""Run Zeroth Check for a composite action and append exactly one summary."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="zeroth-check.yaml")
    parser.add_argument("--report-dir", default=".zeroth/check/reports")
    parser.add_argument("--fail-on", default="block,invalid")
    parser.add_argument(
        "--cli", default=shlex.join([sys.executable, "-m", "zeroth.service.cli"])
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_dir = Path(args.report_dir).resolve()
    command = [
        *shlex.split(args.cli),
        "check",
        "run",
        "--config",
        args.config,
        "--report-dir",
        str(report_dir),
    ]
    completed = subprocess.run(command, check=False)
    summary = report_dir / "check-summary.md"
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary and summary.exists():
        with Path(github_summary).open("ab") as destination:
            destination.write(summary.read_bytes())
    fail_on = {item.strip() for item in args.fail_on.split(",") if item.strip()}
    if completed.returncode == 10 and "canary" not in fail_on:
        return 0
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())

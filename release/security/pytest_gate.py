"""Run one exact matrix tier in one portable pytest invocation."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest

from release.security.matrix import load_matrix


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--tier", choices=("pr-critical", "release-candidate"), required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--junitxml", type=Path, required=True)
    parser.add_argument("--pytest-arg", action="append", default=[])
    args = parser.parse_args(argv)
    nodes = load_matrix(args.matrix).nodes(args.tier)
    return pytest.main(
        [
            "-p",
            "release.security.pytest_plugin",
            f"--security-matrix={args.matrix}",
            f"--security-tier={args.tier}",
            f"--security-results={args.results}",
            f"--junitxml={args.junitxml}",
            *args.pytest_arg,
            *nodes,
        ]
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

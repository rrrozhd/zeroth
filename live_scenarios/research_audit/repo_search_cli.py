"""Wrapped-command executable unit for bounded repository search."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from live_scenarios.research_audit.contracts import (
    RepoSearchInput,
    RepoSearchMatch,
    RepoSearchOutput,
)


def main() -> int:
    payload = json.load(sys.stdin)
    data = RepoSearchInput.model_validate(payload)
    repo_path = Path(data.repo_path).expanduser().resolve()
    result = subprocess.run(
        [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(data.max_matches),
            data.query,
            str(repo_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise SystemExit(result.stderr or f"rg failed with exit code {result.returncode}")
    matches: list[RepoSearchMatch] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path_text, line_text, content = line.split(":", 2)
        matches.append(
            RepoSearchMatch(
                path=str(Path(path_text).resolve()),
                line=int(line_text),
                text=content,
            )
        )
    print(
        RepoSearchOutput(
            query=data.query,
            repo_path=str(repo_path),
            matches=matches,
        ).model_dump_json()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Write one atomic receipt from the exact source and raw load observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.load.environment import observation_digest, runtime_environment  # noqa: E402

REVISION = re.compile(r"[0-9a-f]{40}")


def source_digest(root: Path) -> str:
    """Hash the extracted Git source tree without trusting checkout metadata."""
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def build_receipt(source: Path, raw: Path, commit: str, tree: str) -> dict:
    """Derive the receipt inside the same environment that executed the probe."""
    if REVISION.fullmatch(commit) is None or REVISION.fullmatch(tree) is None:
        raise ValueError("receipt commit/tree is malformed")
    observations = json.loads(raw.read_text(encoding="utf-8"))
    if not isinstance(observations, list) or not observations:
        raise ValueError("receipt observations are empty")
    from zeroth.service import app

    origin = Path(app.__file__).resolve()
    origin.relative_to(source.resolve())
    package = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        "commit": commit,
        "tree": tree,
        "package_version": package["project"]["version"],
        "product_import_origin": str(origin),
        "source_digest": source_digest(source),
        "observation_digest": observation_digest(observations),
        "environment": runtime_environment(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_receipt(args.source, args.raw, args.commit, args.tree)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

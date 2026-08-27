"""Import-order-safe launcher for the provider-free context checkpoint."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--external-root", type=Path, required=True)
    known, _ = parser.parse_known_args(argv)
    root = known.external_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "connector-spool").mkdir(exist_ok=True)

    # These must be pinned before importing release.live_evaluation: its public
    # package facade reaches the embedded economics plane during initialization.
    os.environ["ZEROTH_DATABASE__BACKEND"] = "sqlite"
    os.environ["ZEROTH_DATABASE__SQLITE_PATH"] = str(known.database)
    os.environ["ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR"] = str(root / "artifacts")
    os.environ["ECP_DATABASE_URL"] = f"sqlite+pysqlite:///{root / 'econ-plane.sqlite3'}"
    os.environ["ECP_CONNECTOR_SPOOL_ROOT"] = str(root / "connector-spool")

    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from release.live_evaluation.context_compaction_service import main as service_main

    return service_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

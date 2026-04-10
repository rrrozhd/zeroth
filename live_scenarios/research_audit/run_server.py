"""Run the live research-audit FastAPI scenario with Uvicorn."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from live_scenarios.research_audit.bootstrap import bootstrap_research_audit_app
from zeroth.core.storage import SQLiteDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=str(Path("live_scenarios") / "research_audit.sqlite3"),
        help="SQLite database path for the scenario deployment.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8011, type=int)
    parser.add_argument(
        "--strict-policy",
        action="store_true",
        help="Enable strict policy mode to force early policy denial.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(db_path)
    app = bootstrap_research_audit_app(
        database,
        strict_policy=args.strict_policy,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

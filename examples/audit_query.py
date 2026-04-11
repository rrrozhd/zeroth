"""Query the audit trail for a run — example for docs/how-to/cookbook/audit-query.md.

Writes a handful of :class:`NodeAuditRecord` rows against an in-memory
SQLite database, then shows the two standard query paths:

1. ``AuditRepository.list_by_run(run_id)`` — every record for a run.
2. ``AuditRepository.list(AuditQuery(run_id=..., node_id=...))`` — filtered.

Runs fully in-process; no HTTP or live service needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


async def _run_demo() -> int:
    from zeroth.core.audit import AuditQuery, AuditRepository, NodeAuditRecord
    from zeroth.core.service.bootstrap import run_migrations
    from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "audit.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)
        repo = AuditRepository(database)

        run_id = "run-demo-42"
        graph_version_ref = "demo-graph@1"
        deployment_ref = "demo-deploy"

        # Seed three records on one run: agent -> approval -> tool.
        for idx, node_id in enumerate(["agent", "approval", "tool"]):
            await repo.write(
                NodeAuditRecord(
                    audit_id=f"audit-{idx}",
                    run_id=run_id,
                    node_id=node_id,
                    graph_version_ref=graph_version_ref,
                    deployment_ref=deployment_ref,
                    status="completed",
                    input_snapshot={"message": f"step {idx}"},
                    output_snapshot={"message": f"step {idx} ok"},
                )
            )

        # 1. Walk the whole trail for the run.
        trail = await repo.list_by_run(run_id)
        print(f"full trail for run {run_id}: {len(trail)} records")
        for rec in trail:
            print(f"  [{rec.node_id}] status={rec.status} audit_id={rec.audit_id}")

        # 2. Filtered query — just the approval node's records.
        approvals = await repo.list(AuditQuery(run_id=run_id, node_id="approval"))
        print(f"approval-only slice: {len(approvals)} record(s)")
        assert len(approvals) == 1
        assert approvals[0].node_id == "approval"

    print("audit-query demo OK")
    return 0


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    sys.exit(main())

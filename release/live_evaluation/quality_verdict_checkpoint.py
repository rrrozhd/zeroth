"""Seal the native-Safari quality-verdict non-rewriting checkpoint.

The reviewer label is intentionally outside the audit chain: it interprets an
already-terminal outcome and must not rewrite execution, checkpoint, receipt,
action, or economics evidence. This checkpoint snapshots those surfaces before
and after the real UI submission and fails closed on any extra mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, EvidenceStore


def _canonical(value: object) -> bytes:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(connection: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[dict]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def _parsed_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _api_json(base: str, key: str, path: str) -> dict:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Accept": "application/json", "X-API-Key": key},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - loopback gate
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError(f"expected object response from {path}")
    return payload


def _table_digest(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    rows = _rows(connection, f'SELECT * FROM "{table}" ORDER BY 1')  # noqa: S608
    return {"count": len(rows), "digest": _digest(rows)}


def capture_snapshot(
    *,
    state_root: Path,
    run_id: str,
    api_base: str,
) -> dict[str, object]:
    """Capture redacted API and persistence identities for one terminal run."""
    key = (state_root / "runtime-secrets/service-api-key").read_text().strip()
    if not key:
        raise RuntimeError("service key is empty")

    service_db = state_root / "zeroth.db"
    econ_db = state_root / "econ.db"
    sink_db = state_root / "action-sink/actions.sqlite3"

    with sqlite3.connect(service_db) as service:
        run_rows = _rows(service, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if len(run_rows) != 1:
            raise RuntimeError(f"expected exactly one scoped run row for {run_id}")
        run_row = run_rows[0]
        thread_rows = _rows(
            service,
            "SELECT * FROM threads WHERE thread_id = ? AND tenant_id = ?",
            (run_row["thread_id"], run_row["tenant_id"]),
        )
        checkpoint_rows = _rows(
            service,
            "SELECT * FROM run_checkpoints WHERE run_id = ? AND tenant_id = ? "
            "ORDER BY checkpoint_order",
            (run_id, run_row["tenant_id"]),
        )
        audit_rows = _rows(
            service,
            "SELECT audit_id, run_id, thread_id, node_id, graph_version_ref, "
            "deployment_ref, tenant_id, workspace_id, created_at, cost_usd, "
            "cost_event_id, chain_sequence, record_json FROM node_audits "
            "WHERE run_id = ? ORDER BY created_at, audit_id",
            (run_id,),
        )
        for row in audit_rows:
            row["record_json"] = "sha256:" + hashlib.sha256(
                str(row["record_json"]).encode()
            ).hexdigest()
        operation_rows = _rows(
            service,
            "SELECT operation_key, run_id, dispatch_id, idempotency_key, target_ref, "
            "attempt, state, support, receipt, error, ambiguity_reason, "
            "reconciliation_attempts, created_at, updated_at, tenant_id, workspace_id "
            "FROM side_effect_operations WHERE run_id = ? ORDER BY operation_key",
            (run_id,),
        )
        for row in operation_rows:
            if row["receipt"] is not None:
                row["receipt"] = "sha256:" + hashlib.sha256(
                    str(row["receipt"]).encode()
                ).hexdigest()

    run_metadata = _parsed_json(run_row.pop("metadata"))
    run_execution_digest = _digest(run_row)

    operation_keys = [str(row["operation_key"]) for row in operation_rows]
    marker_rows: list[dict] = []
    if operation_keys and sink_db.is_file():
        placeholders = ",".join("?" for _ in operation_keys)
        with sqlite3.connect(sink_db) as sink:
            marker_rows = _rows(
                sink,
                f"SELECT operation_key, payload_hash, receipt, created_at FROM action_markers "
                f"WHERE operation_key IN ({placeholders}) ORDER BY operation_key",  # noqa: S608
                operation_keys,
            )
        for row in marker_rows:
            row["receipt"] = "sha256:" + hashlib.sha256(str(row["receipt"]).encode()).hexdigest()

    economics_tables: dict[str, object] = {}
    with sqlite3.connect(econ_db) as economics:
        for table in (
            "execution_events",
            "cost_reservations",
            "cost_estimates",
            "outcome_events",
            "audit_log",
        ):
            economics_tables[table] = _table_digest(economics, table)

    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "api": {
            "run": _api_json(api_base, key, f"/v1/runs/{run_id}"),
            "unit_economics": _api_json(
                api_base, key, "/v1/econ/unit-economics?scope=tenant"
            ),
            "rightsizing_opportunities": _api_json(
                api_base, key, "/v1/econ/rightsizing/opportunities"
            ),
        },
        "database": {
            "run_execution_digest": run_execution_digest,
            "run_metadata": run_metadata,
            "thread_digest": _digest(thread_rows),
            "thread_rows": len(thread_rows),
            "checkpoints_digest": _digest(checkpoint_rows),
            "checkpoint_rows": len(checkpoint_rows),
            "audits_digest": _digest(audit_rows),
            "audit_rows": len(audit_rows),
            "operations_digest": _digest(operation_rows),
            "operation_rows": len(operation_rows),
            "action_markers_digest": _digest(marker_rows),
            "action_marker_rows": len(marker_rows),
            "economics_tables": economics_tables,
        },
    }


def _run_identity(run: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "run_id",
        "thread_id",
        "status",
        "graph_version_ref",
        "deployment_ref",
        "final_output",
        "audit_refs",
        "execution_history",
    )
    return {field: run.get(field) for field in fields}


def compare_snapshots(before: Mapping[str, Any], after: Mapping[str, Any], *, run_id: str) -> dict:
    before_db = before["database"]
    after_db = after["database"]
    before_api = before["api"]
    after_api = after["api"]
    before_metadata = dict(before_db["run_metadata"] or {})
    after_metadata = dict(after_db["run_metadata"] or {})
    verdict = after_metadata.pop("quality_verdict", None)

    before_econ = before_api["unit_economics"]
    after_econ = after_api["unit_economics"]
    stable_econ_fields = (
        "total_cost_usd",
        "terminal_cost_usd",
        "failure_tax_usd",
        "failure_tax_ratio",
        "runs_with_cost",
        "by_workflow",
        "by_tenant",
    )
    verdict_valid = isinstance(verdict, Mapping) and (
        verdict.get("verdict") == "good"
        and verdict.get("source") == "human:console"
        and bool(verdict.get("attached_at"))
    )
    before_labels = int(before_econ["quality"]["labeled_terminal_runs"])
    after_labels = int(after_econ["quality"]["labeled_terminal_runs"])

    checks = {
        "run_id_matches": before.get("run_id") == after.get("run_id") == run_id,
        "only_quality_metadata_added": after_metadata == before_metadata and verdict_valid,
        "run_execution_unchanged": (
            before_db["run_execution_digest"] == after_db["run_execution_digest"]
        ),
        "api_run_identity_unchanged": (
            _run_identity(before_api["run"]) == _run_identity(after_api["run"])
        ),
        "thread_unchanged": before_db["thread_digest"] == after_db["thread_digest"],
        "checkpoints_unchanged": (
            before_db["checkpoints_digest"] == after_db["checkpoints_digest"]
        ),
        "audits_unchanged": before_db["audits_digest"] == after_db["audits_digest"],
        "operations_unchanged": (
            before_db["operations_digest"] == after_db["operations_digest"]
        ),
        "action_markers_unchanged": (
            before_db["action_markers_digest"] == after_db["action_markers_digest"]
        ),
        "economics_tables_unchanged": (
            before_db["economics_tables"] == after_db["economics_tables"]
        ),
        "cost_totals_unchanged": all(
            before_econ.get(field) == after_econ.get(field) for field in stable_econ_fields
        ),
        "quality_label_count_incremented_once": after_labels == before_labels + 1,
        "quality_overlay_is_honest": after_econ["quality"]["state"] in {
            "below_coverage_floor",
            "ok",
        },
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "passed": all(checks.values()),
        "checks": checks,
        "before_quality": before_econ["quality"],
        "after_quality": after_econ["quality"],
        "verdict": verdict,
    }


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout


def _record_command(
    store: EvidenceStore, sequence: int, name: str, argv: list[str], cwd: Path
) -> None:
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {name}")


def prepare(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    if root.exists():
        raise RuntimeError(f"checkpoint already exists: {root}")
    store = EvidenceStore(root)
    diff = _git(args.worktree, "diff", "--binary", "HEAD")
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "rightsizing-quality-verdict-nonrewriting",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _git(args.worktree, "rev-parse", "HEAD").strip(),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "run_id": args.run_id,
            "tenant_id": "evaluation-studio-v1",
            "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
            "runtime": {"python": platform.python_version(), "platform": platform.platform()},
            "limits": {"campaign_usd": "10.00", "per_run_usd": "0.25"},
            "limitations": [
                "no provider call is made by this checkpoint",
                "measured Rightsizing remains blocked pending a rotated external credential",
                "the quality verdict is an annotation and deliberately not an audit event",
            ],
        }
    )
    before = capture_snapshot(
        state_root=args.state_root.resolve(), run_id=args.run_id, api_base=args.api_base
    )
    store._write_exclusive(Path("runtime/before.json"), before)
    store.append_event(
        "run.snapshot.before",
        {"evidence_path": "runtime/before.json", "phase": "before-verdict"},
        correlation=args.correlation,
    )


def ingest(args: argparse.Namespace) -> None:
    store = EvidenceStore(args.root.resolve())
    store.ingest_artifact(args.source.resolve(), f"screenshots/{args.name}.jpeg")


def finalize(args: argparse.Namespace) -> None:
    store = EvidenceStore(args.root.resolve())
    after = capture_snapshot(
        state_root=args.state_root.resolve(), run_id=args.run_id, api_base=args.api_base
    )
    store._write_exclusive(Path("runtime/after.json"), after)
    before = json.loads((store.root / "runtime/before.json").read_text())
    comparison = compare_snapshots(before, after, run_id=args.run_id)
    store._write_exclusive(Path("runtime/comparison.json"), comparison)
    if not comparison["passed"]:
        raise RuntimeError("quality-verdict non-rewriting comparison failed")

    _record_command(
        store,
        1,
        "quality-verdict-backend-tests",
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/live_evaluation/test_quality_verdict_checkpoint.py",
            "tests/test_econ_analytics_api.py",
            "tests/integrations/persistence/runs/test_run_repository.py",
        ],
        args.worktree,
    )
    _record_command(
        store,
        2,
        "rightsizing-frontend-tests",
        ["npm", "test", "--", "--run", "app/layout-accessibility.test.ts"],
        args.worktree / "frontend",
    )
    _record_command(
        store,
        3,
        "frontend-typescript",
        ["npm", "exec", "tsc", "--", "--noEmit"],
        args.worktree / "frontend",
    )
    _record_command(
        store,
        4,
        "quality-verdict-ruff",
        [
            "uv",
            "run",
            "ruff",
            "check",
            "release/live_evaluation/quality_verdict_checkpoint.py",
            "src/zeroth/integrations/persistence/runs/run_repository.py",
            "src/zeroth/service/api/econ_analytics_api.py",
            "tests/live_evaluation/test_quality_verdict_checkpoint.py",
            "tests/test_econ_analytics_api.py",
            "tests/integrations/persistence/runs/test_run_repository.py",
        ],
        args.worktree,
    )

    evidence = (
        "runtime/before.json",
        "runtime/after.json",
        "runtime/comparison.json",
        "screenshots/01-rightsizing-painted.jpeg",
        "screenshots/02-verdict-configured.jpeg",
        "screenshots/03-verdict-attached.jpeg",
        "screenshots/04-refresh-restored.jpeg",
        "commands/0001-quality-verdict-backend-tests.json",
        "commands/0002-rightsizing-frontend-tests.json",
        "commands/0003-frontend-typescript.json",
        "commands/0004-quality-verdict-ruff.json",
    )
    criterion = AcceptanceCriterion(
        "economics.quality-verdict-nonrewriting",
        "pass",
        evidence,
        note=(
            "Native Safari attached one reviewer verdict to an existing terminal run. "
            "Only runs.metadata changed; execution, checkpoint, audit, receipt, action, "
            "and economics identities remained unchanged."
        ),
    )
    report = f"""# Rightsizing quality-verdict checkpoint

Native Safari attached one `good` verdict to run `{args.run_id}` through the
Rightsizing correctness-mode form. The quality overlay advanced from zero to
one labeled terminal run. The run, thread, checkpoints, signed audit rows,
side-effect operation, durable action marker, and all economics tables retained
their exact pre-submit digests.

This passes only `economics.quality-verdict-nonrewriting`. It does not claim a
measured model comparison or actual provider spend; those remain blocked until
a newly rotated credential is supplied through the external secret provider.
"""
    store.finalize_bundle(acceptance=(criterion,), report_markdown=report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "ingest", "finalize"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--api-base", default="http://127.0.0.1:8122")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--name")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "ingest":
        if args.source is None or args.name is None:
            raise ValueError("ingest requires --source and --name")
        ingest(args)
        return 0
    if args.state_root is None or args.run_id is None:
        raise ValueError(f"{args.command} requires --state-root and --run-id")
    # Imported lazily so unit tests of compare_snapshots do not construct IDs.
    from .evidence import CorrelationIds

    args.correlation = CorrelationIds(run_id=args.run_id)
    if args.command == "prepare":
        prepare(args)
    else:
        finalize(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

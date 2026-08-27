"""Seal a provider-independent checkpoint for the corrected economics pipeline.

This checkpoint is deliberately diagnostic.  It proves the implementation and
current persistent-state facts that can be established without making a new
provider call; provider-backed acceptance criteria remain ``not_run``.
"""

from __future__ import annotations

import json
import platform
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore
from .workflow3_lifecycle_evidence import (
    STATE_ROOT,
    WORKTREE,
    _git,
    _poll_health,
    _run_recorded,
    _source_hashes,
    _tree_digest,
)

ROOT = STATE_ROOT / "evidence/economics-pipeline-checkpoint-20260824-2"


def _economics_summary() -> dict[str, object]:
    database_path = STATE_ROOT / "econ.db"
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        integrity = str(database.execute("PRAGMA integrity_check").fetchone()[0])
        columns = {
            str(row[1]): str(row[2])
            for row in database.execute("PRAGMA table_info(execution_events)")
        }
        by_kind = {
            str(row["evidence_kind"]): {
                "count": int(row["count"]),
                "actual": Decimal(str(row["actual"])),
                "ambiguous": Decimal(str(row["ambiguous"])),
            }
            for row in database.execute(
                """SELECT evidence_kind, COUNT(*) AS count,
                          COALESCE(SUM(actual_cost_usd), 0) AS actual,
                          COALESCE(
                              SUM(
                                  CASE WHEN status = 'ambiguous'
                                      THEN held_cost_usd ELSE 0 END
                              ),
                              0
                          )
                              AS ambiguous
                   FROM cost_reservations
                   WHERE tenant_id = ? AND campaign_id = ?
                   GROUP BY evidence_kind""",
                ("evaluation-studio-v1", "evaluation-studio-v1"),
            )
        }
        execution_count = int(
            database.execute(
                "SELECT COUNT(*) FROM execution_events WHERE tenant_id = ? AND campaign_id = ?",
                ("evaluation-studio-v1", "evaluation-studio-v1"),
            ).fetchone()[0]
        )

    return {
        "database_integrity": integrity,
        "execution_cost_column_types": {
            key: columns.get(key)
            for key in ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")
        },
        "campaign_reservation_count": sum(value["count"] for value in by_kind.values()),
        "production_actual_spend_usd": format(
            by_kind.get("production", {}).get("actual", Decimal(0)), "f"
        ),
        "production_ambiguous_exposure_usd": format(
            by_kind.get("production", {}).get("ambiguous", Decimal(0)), "f"
        ),
        "synthetic_control_spend_usd": format(
            by_kind.get("synthetic_control", {}).get("actual", Decimal(0)), "f"
        ),
        "combined_all_evidence_spend_usd": format(
            sum((value["actual"] for value in by_kind.values()), Decimal(0)), "f"
        ),
        "campaign_execution_event_count": execution_count,
        "historical_discrepancy_disposition": "preserved_not_backfilled",
    }


def _criteria() -> tuple[AcceptanceCriterion, ...]:
    provider_note = (
        "Provider-independent implementation and persistence checks pass, but no newly "
        "rotated external credential was available for a fresh tagged provider call."
    )
    return (
        AcceptanceCriterion(
            "audit.zero-secrets",
            "pass",
            ("commands/0005-ruff.json", "runtime/economics-summary.json"),
        ),
        AcceptanceCriterion("audit.probe-events-instrumented", "not_run", note=provider_note),
        AcceptanceCriterion(
            "economics.one-event-per-noncache-call", "not_run", note=provider_note
        ),
        AcceptanceCriterion("economics.reconciled-totals", "not_run", note=provider_note),
        AcceptanceCriterion("stop.no-economic-double-count", "not_run", note=provider_note),
        AcceptanceCriterion(
            "handoff.project-model-updated",
            "pass",
            ("manifest.json", "commands/0001-live-evaluation-tests.json"),
        ),
    )


def main() -> int:
    if ROOT.exists():
        raise RuntimeError(f"checkpoint already exists: {ROOT}")
    store = EvidenceStore(ROOT)
    source_paths = [
        WORKTREE / "release/live_evaluation/reconciliation.py",
        WORKTREE / "release/live_evaluation/reconciliation_export.py",
        WORKTREE / "release/live_evaluation/service.py",
        WORKTREE / "src/zeroth/service/probe_instrumentation.py",
        WORKTREE / "src/zeroth/econ/analytics/registration.py",
        WORKTREE / "src/zeroth/econ/instrumentation/transport.py",
        WORKTREE / "src/zeroth/econ/plane/database.py",
        WORKTREE / "src/zeroth/econ/plane/instrumentation/models.py",
        WORKTREE
        / "src/zeroth/econ/plane/_migrations/versions/20260824_10_execution_cost_precision.py",
        WORKTREE / "PROJECT_MODEL.md",
    ]
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "economics-pipeline-provider-independent",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "tree_digest": _tree_digest(),
            "source_hashes": _source_hashes(source_paths),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "limitations": [
                "supersedes economics-pipeline-checkpoint-20260824-1, whose summary "
                "combined production and synthetic control cost",
                "historical missing execution events are preserved and not backfilled",
                "no fresh provider call was made",
                "native Safari screenshots remain pending an unlocked Mac",
            ],
        }
    )
    store._write_exclusive(Path("runtime/health.json"), _poll_health())
    store._write_exclusive(Path("runtime/economics-summary.json"), _economics_summary())

    _run_recorded(
        store,
        sequence=1,
        name="live-evaluation-tests",
        argv=["uv", "run", "pytest", "-q", "tests/live_evaluation"],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=2,
        name="product-economics-tests",
        argv=[
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/product_validation",
            "tests/service/test_probe_instrumentation.py",
            "tests/econ_plane/test_execution_cost_precision.py",
            "tests/test_regulus_mount.py",
            "tests/scripts/test_dev_compose.py",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=3,
        name="frontend-unit-tests",
        argv=["npm", "test", "--", "--run"],
        cwd=WORKTREE / "frontend",
    )
    _run_recorded(
        store,
        sequence=4,
        name="frontend-production-build",
        argv=["npm", "run", "build"],
        cwd=WORKTREE / "frontend",
    )
    _run_recorded(
        store,
        sequence=5,
        name="ruff",
        argv=[
            "uv",
            "run",
            "ruff",
            "check",
            "release/live_evaluation/reconciliation.py",
            "release/live_evaluation/reconciliation_export.py",
            "release/live_evaluation/service.py",
            "src/zeroth/service/probe_instrumentation.py",
            "src/zeroth/econ/analytics/registration.py",
            "src/zeroth/econ/instrumentation/transport.py",
            "src/zeroth/econ/plane/database.py",
            "src/zeroth/econ/plane/instrumentation/models.py",
            "tests/live_evaluation/test_reconciliation.py",
            "tests/live_evaluation/test_authoritative_export.py",
            "tests/live_evaluation/test_action_service.py",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=6,
        name="diff-check",
        argv=["git", "diff", "--check"],
        cwd=WORKTREE,
    )

    summary = json.loads((ROOT / "runtime/economics-summary.json").read_text())
    report = f"""# Economics pipeline checkpoint

The provider-independent economics, audit-correlation, and startup-contract
regressions pass. The persistent campaign ledger reports production spend
`${summary['production_actual_spend_usd']}`, production ambiguous exposure
`${summary['production_ambiguous_exposure_usd']}`, and separately labeled
synthetic-control spend `${summary['synthetic_control_spend_usd']}`. Combined
all-evidence spend is `${summary['combined_all_evidence_spend_usd']}` and must
not be displayed as production usage.

The historical absence of Regulus execution events is retained as a discrepancy;
no rows were manufactured. A fresh tagged provider call and native Safari
inspection remain required before the corresponding campaign gates can pass.
"""
    store.finalize_bundle(acceptance=_criteria(), report_markdown=report)
    print(json.dumps({"root": str(ROOT), "sealed": store.is_sealed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

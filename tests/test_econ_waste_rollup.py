"""Tests for the deployment-wide waste rollup (ECON-WASTE-02)."""

from __future__ import annotations

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.core.econ.waste import WasteKind, waste_rollup
from zeroth.core.runs.models import Run, RunStatus


def _run(run_id: str, status: RunStatus, *, parent: str | None = None) -> Run:
    return Run(
        run_id=run_id,
        graph_version_ref="g",
        deployment_ref="default",
        status=status,
        parent_run_id=parent,
    )


def _audit(
    run_id: str, cost: float, *, node_id: str = "agent", suffix: str = ""
) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=f"{run_id}-{node_id}-{cost}{suffix}",
        run_id=run_id,
        node_id=node_id,
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=cost,
    )


def test_empty_reports_no_history():
    r = waste_rollup([], [])
    assert r.window_runs == 0
    assert "no runs" in r.note.lower()


def test_failed_run_is_confirmed_waste():
    r = waste_rollup([_run("f1", RunStatus.FAILED)], [_audit("f1", 0.20)])
    assert r.total_confirmed_waste_usd == 0.20
    assert r.total_flagged_waste_usd == 0.0
    assert r.runs_with_waste == 1
    assert r.waste_ratio == 1.0
    assert r.top_findings[0].kind == WasteKind.PAID_FOR_FAILED_RUN
    assert r.top_findings[0].run_id == "f1"  # finding stays traceable to its run


def test_loop_in_completed_run_is_flagged_not_confirmed():
    # A completed run whose node executed twice: recoverable-if-unintended → flagged.
    runs = [_run("c1", RunStatus.COMPLETED)]
    audits = [_audit("c1", 0.10, suffix="a"), _audit("c1", 0.10, suffix="b")]
    r = waste_rollup(runs, audits)
    assert r.total_confirmed_waste_usd == 0.0
    assert r.total_flagged_waste_usd == 0.10  # node spend minus its most-expensive execution
    assert r.top_findings[0].kind == WasteKind.LOOP_REEXECUTION


def test_no_cost_note_is_cause_neutral():
    r = waste_rollup([_run("c1", RunStatus.COMPLETED)], [])
    assert r.runs_with_cost == 0
    assert "no priced model spend" in r.note.lower()
    assert "regulus" not in r.note.lower()


def test_subgraph_child_runs_excluded():
    runs = [_run("p1", RunStatus.FAILED), _run("child", RunStatus.FAILED, parent="p1")]
    audits = [_audit("p1", 0.20), _audit("child", 0.05)]
    r = waste_rollup(runs, audits)
    assert r.window_runs == 1  # only the parent
    assert r.total_confirmed_waste_usd == 0.20  # child's $0.05 not attributed at top level


def test_by_kind_totals_and_ranking():
    runs = [_run("f1", RunStatus.FAILED), _run("f2", RunStatus.FAILED)]
    audits = [_audit("f1", 0.30), _audit("f2", 0.10)]
    r = waste_rollup(runs, audits)
    by_kind = {k.kind: k for k in r.by_kind}
    assert by_kind[WasteKind.PAID_FOR_FAILED_RUN].count == 2
    assert by_kind[WasteKind.PAID_FOR_FAILED_RUN].wasted_usd == 0.40
    assert r.confirmed_findings == 2
    # Ranked by recoverable dollars: the bigger failed run first.
    assert r.top_findings[0].run_id == "f1"
    assert r.top_findings[0].wasted_usd == 0.30


def test_in_flight_runs_excluded_from_waste():
    # An in-flight (RUNNING) run's spend hasn't resolved to an outcome — it must not enter
    # the rollup (consistent with unit economics excluding in-flight).
    runs = [_run("f1", RunStatus.FAILED), _run("live", RunStatus.RUNNING)]
    audits = [_audit("f1", 0.20), _audit("live", 5.00)]
    r = waste_rollup(runs, audits)
    assert r.window_runs == 1  # only the terminal run
    assert r.total_cost_usd == 0.20  # the running run's $5 is not counted
    assert r.total_confirmed_waste_usd == 0.20


def test_no_waste_when_all_completed_single_pass():
    runs = [_run("c1", RunStatus.COMPLETED)]
    r = waste_rollup(runs, [_audit("c1", 0.10)])
    assert r.total_confirmed_waste_usd == 0.0
    assert r.total_flagged_waste_usd == 0.0
    assert "no economic waste" in r.note.lower()

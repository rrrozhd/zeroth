"""Tests for the quality-aware outcomes overlay (ECON-QUALITY-01)."""

from __future__ import annotations

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.econ.analytics.quality import quality_economics, read_quality_verdict
from zeroth.core.runs.models import Run, RunStatus


def _run(
    run_id: str,
    status: RunStatus,
    *,
    verdict: str | None = None,
    source: str = "human",
    parent: str | None = None,
) -> Run:
    r = Run(
        run_id=run_id,
        graph_version_ref="g",
        deployment_ref="default",
        status=status,
        parent_run_id=parent,
    )
    if verdict is not None:
        r.metadata = {**(r.metadata or {}), "quality_verdict": {"verdict": verdict, "source": source}}
    return r


def _audit(run_id: str, cost: float) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=f"{run_id}-{cost}",
        run_id=run_id,
        node_id="agent",
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=cost,
    )


def test_no_verdicts_is_not_configured():
    runs = [_run("s1", RunStatus.COMPLETED), _run("s2", RunStatus.COMPLETED)]
    report = quality_economics(runs, [_audit("s1", 0.10), _audit("s2", 0.10)])
    assert report.state == "not_configured"
    assert report.cost_per_quality_success_usd is None
    assert report.labeled_terminal_runs == 0
    assert "no quality verdicts" in report.note.lower()


def test_labeled_mix_loads_bad_cost_onto_good_outcomes():
    runs = [
        _run("g1", RunStatus.COMPLETED, verdict="good"),
        _run("b1", RunStatus.COMPLETED, verdict="bad"),
    ]
    audits = [_audit("g1", 0.10), _audit("b1", 0.20)]
    report = quality_economics(runs, audits)
    assert report.state == "ok"
    assert report.coverage == 1.0
    assert report.quality_successes == 1
    # labeled spend (0.30) loaded onto the 1 good outcome.
    assert report.cost_per_quality_success_usd == 0.30
    assert report.cost_on_quality_failures_usd == 0.20
    assert report.quality_success_rate_over_labeled == 0.5
    assert report.sources == ["human"]


def test_unlabeled_run_excluded_from_both_numerator_and_denominator():
    runs = [
        _run("g1", RunStatus.COMPLETED, verdict="good"),
        _run("b1", RunStatus.COMPLETED, verdict="bad"),
        _run("u1", RunStatus.COMPLETED),  # unlabeled, expensive
    ]
    audits = [_audit("g1", 0.10), _audit("b1", 0.20), _audit("u1", 5.00)]
    report = quality_economics(runs, audits)
    assert report.terminal_runs == 3
    assert report.labeled_terminal_runs == 2
    # u1's $5 must NOT enter the labeled cost.
    assert report.cost_per_quality_success_usd == 0.30
    assert round(report.coverage, 2) == 0.67


def test_below_coverage_floor_is_none():
    runs = [_run(f"r{i}", RunStatus.COMPLETED) for i in range(9)]
    runs.append(_run("g1", RunStatus.COMPLETED, verdict="good"))
    audits = [_audit("g1", 0.10)]
    report = quality_economics(runs, audits, min_coverage=0.2)
    assert report.state == "below_coverage_floor"  # 1/10 = 0.1 < 0.2
    assert report.cost_per_quality_success_usd is None


def test_unknown_verdict_is_excluded():
    runs = [_run("x1", RunStatus.COMPLETED, verdict="unknown")]
    report = quality_economics(runs, [_audit("x1", 0.10)])
    assert report.labeled_terminal_runs == 0
    assert report.state == "not_configured"


def test_in_flight_run_never_enters_the_metric():
    runs = [_run("live", RunStatus.RUNNING, verdict="good")]
    report = quality_economics(runs, [_audit("live", 0.10)])
    assert report.terminal_runs == 0
    assert report.labeled_terminal_runs == 0


def test_read_quality_verdict_is_defensive():
    good = _run("g", RunStatus.COMPLETED, verdict="good")
    assert read_quality_verdict(good).verdict == "good"
    # Absent, garbage, and wrong-shape all return None — never raise, never default good.
    assert read_quality_verdict(_run("n", RunStatus.COMPLETED)) is None
    garbage = Run(run_id="x", graph_version_ref="g", deployment_ref="default", status=RunStatus.COMPLETED)
    garbage.metadata = {"quality_verdict": "not-a-dict"}
    assert read_quality_verdict(garbage) is None
    wrong = Run(run_id="y", graph_version_ref="g", deployment_ref="default", status=RunStatus.COMPLETED)
    wrong.metadata = {"quality_verdict": {"verdict": "excellent", "source": "x"}}  # invalid literal
    assert read_quality_verdict(wrong) is None

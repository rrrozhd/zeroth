"""Tests for unit economics over the run trail (ECON-UNIT-01).

Pure aggregation: synthetic ``Run`` objects (authoritative status) joined with synthetic
``NodeAuditRecord`` costs. No litellm, no network. Asserts the honesty rails hold:
in-flight spend never enters the headline, a zero success denominator stays ``None``, and
"runs but no cost" is reported as disabled tracking rather than a misleading $0.
"""

from __future__ import annotations

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.econ.analytics.unit_economics import unit_economics
from zeroth.runtime.runs import Run
from zeroth.runtime.runs import RunStatus


def _run(
    run_id: str,
    status: RunStatus,
    *,
    workflow: str = "checkout",
    tenant: str = "default",
    parent: str | None = None,
) -> Run:
    return Run(
        run_id=run_id,
        graph_version_ref="g",
        deployment_ref="default",
        workflow_name=workflow,
        tenant_id=tenant,
        status=status,
        parent_run_id=parent,
    )


def _audit(
    run_id: str,
    cost: float | None,
    *,
    estimated_cost: float | None = None,
    node_id: str = "agent",
) -> NodeAuditRecord:
    return NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id=f"{run_id}-{node_id}-{cost}",
        run_id=run_id,
        node_id=node_id,
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=cost,
        estimated_cost_usd=estimated_cost,
    )


def test_cost_per_successful_run_loads_the_failure_tax():
    runs = [
        _run("s1", RunStatus.COMPLETED),
        _run("s2", RunStatus.COMPLETED),
        _run("f1", RunStatus.FAILED),
    ]
    audits = [
        _audit("s1", 0.10),
        _audit("s2", 0.10),
        _audit("f1", 0.20),  # paid, produced nothing
    ]
    report = unit_economics(runs, audits)

    assert report.successful_runs == 2
    assert report.failed_runs == 1
    assert report.success_rate == round(2 / 3, 4)
    assert report.terminal_cost_usd == 0.40
    assert report.cost_on_failed_usd == 0.20
    assert report.failure_tax_usd == 0.20
    assert report.failure_tax_ratio == 0.5  # half of terminal spend bought nothing
    # Headline loads the $0.20 failure tax onto the 2 good outcomes: 0.40 / 2.
    assert report.cost_per_successful_run_usd == 0.20
    # Clean cost excludes the tax: 0.20 / 2.
    assert report.mean_cost_per_successful_run_usd == 0.10


def test_in_flight_run_excluded_from_headline_but_totals_reconcile():
    runs = [
        _run("s1", RunStatus.COMPLETED),
        _run("live", RunStatus.RUNNING),
    ]
    audits = [_audit("s1", 0.10), _audit("live", 5.00)]  # a big in-flight spend
    report = unit_economics(runs, audits)

    assert report.in_flight_runs == 1
    assert report.cost_on_in_flight_usd == 5.00
    # The running run's $5 must NOT inflate the cost of a good outcome.
    assert report.cost_per_successful_run_usd == 0.10
    assert report.terminal_cost_usd == 0.10
    # total reconciles: terminal + in-flight.
    assert report.total_cost_usd == 5.10


def test_no_successful_runs_leaves_headline_none():
    runs = [_run("f1", RunStatus.FAILED), _run("f2", RunStatus.FAILED)]
    audits = [_audit("f1", 0.20), _audit("f2", 0.20)]
    report = unit_economics(runs, audits)

    assert report.successful_runs == 0
    assert report.cost_per_successful_run_usd is None
    assert report.mean_cost_per_successful_run_usd is None
    assert report.failure_tax_usd == 0.40
    assert "undefined" in report.note.lower()


def test_runs_without_attributed_cost_note_is_cause_neutral():
    # Runs exist but no priced model spend anywhere. The note explains the causes
    # without blaming Regulus (cost attribution is on by default, not Regulus-gated).
    runs = [_run("s1", RunStatus.COMPLETED), _run("s2", RunStatus.COMPLETED)]
    report = unit_economics(runs, [])

    assert report.window_runs == 2
    assert report.runs_with_cost == 0
    assert report.cost_per_successful_run_usd == 0.0  # 0 terminal cost / 2 successes
    assert "no priced model calls" in report.note.lower()
    assert "regulus" not in report.note.lower()


def test_empty_reports_no_history():
    report = unit_economics([], [])
    assert report.window_runs == 0
    assert report.cost_per_successful_run_usd is None
    assert "no runs" in report.note.lower()


def test_subgraph_child_runs_are_not_outcomes():
    # A child run (parent_run_id set) must not count as a separate outcome, and its cost
    # must not be attributed at the top level.
    runs = [
        _run("parent", RunStatus.COMPLETED),
        _run("child", RunStatus.COMPLETED, parent="parent"),
    ]
    audits = [_audit("parent", 0.10), _audit("child", 0.05)]
    report = unit_economics(runs, audits)

    assert report.window_runs == 1  # only the parent
    assert report.successful_runs == 1
    # child's $0.05 is attributed under its own run_id, which is dropped → parent cost only.
    assert report.cost_per_successful_run_usd == 0.10


def test_by_workflow_ranked_by_terminal_spend():
    runs = [
        _run("a1", RunStatus.COMPLETED, workflow="cheap"),
        _run("b1", RunStatus.COMPLETED, workflow="pricey"),
        _run("b2", RunStatus.FAILED, workflow="pricey"),
    ]
    audits = [_audit("a1", 0.01), _audit("b1", 0.40), _audit("b2", 0.30)]
    report = unit_economics(runs, audits)

    assert [w.workflow_name for w in report.by_workflow] == ["pricey", "cheap"]
    pricey = report.by_workflow[0]
    assert pricey.runs == 2
    assert pricey.successful_runs == 1
    assert pricey.terminal_cost_usd == 0.70
    assert pricey.cost_per_successful_run_usd == 0.70  # 0.70 terminal / 1 success
    assert pricey.failure_tax_usd == 0.30


def test_by_tenant_identifies_unprofitable_customer():
    # The literal 'which customer is unprofitable' case: acme succeeds, globex only fails.
    runs = [
        _run("a1", RunStatus.COMPLETED, tenant="acme"),
        _run("g1", RunStatus.FAILED, tenant="globex"),
        _run("g2", RunStatus.FAILED, tenant="globex"),
    ]
    audits = [_audit("a1", 0.10), _audit("g1", 0.20), _audit("g2", 0.15)]
    report = unit_economics(runs, audits)

    by_tenant = {t.tenant_id: t for t in report.by_tenant}
    globex = by_tenant["globex"]
    assert globex.successful_runs == 0
    assert globex.cost_per_successful_run_usd is None  # zero-success customer never faked cheap
    assert globex.failure_tax_usd == 0.35  # pure waste
    assert globex.success_rate == 0.0
    # Ranked by terminal spend: globex ($0.35) ahead of acme ($0.10).
    assert [t.tenant_id for t in report.by_tenant] == ["globex", "acme"]


def test_by_tenant_and_by_workflow_reconcile_to_the_total():
    # Same run population sliced two ways must sum to the same terminal cost.
    runs = [
        _run("a1", RunStatus.COMPLETED, workflow="w1", tenant="t1"),
        _run("b1", RunStatus.FAILED, workflow="w2", tenant="t2"),
    ]
    audits = [_audit("a1", 0.10), _audit("b1", 0.20)]
    report = unit_economics(runs, audits)

    assert round(sum(t.terminal_cost_usd for t in report.by_tenant), 6) == report.terminal_cost_usd
    assert (
        round(sum(w.terminal_cost_usd for w in report.by_workflow), 6) == report.terminal_cost_usd
    )


def test_by_tenant_single_default_tenant_is_one_row():
    runs = [_run("s1", RunStatus.COMPLETED), _run("s2", RunStatus.COMPLETED)]
    report = unit_economics(runs, [_audit("s1", 0.10), _audit("s2", 0.10)])
    assert len(report.by_tenant) == 1
    assert report.by_tenant[0].tenant_id == "default"


def test_audit_for_out_of_window_run_is_ignored():
    # An audit whose run isn't in the run window must not add phantom cost.
    runs = [_run("s1", RunStatus.COMPLETED)]
    audits = [_audit("s1", 0.10), _audit("ancient_run", 9.99)]
    report = unit_economics(runs, audits)

    assert report.total_cost_usd == 0.10
    assert report.cost_per_successful_run_usd == 0.10


def test_failed_only_estimated_spend_is_visible_without_becoming_measured():
    report = unit_economics(
        [_run("f1", RunStatus.FAILED)],
        [_audit("f1", None, estimated_cost=0.20)],
    )

    assert report.total_cost_usd == 0.0
    assert report.failure_tax_usd == 0.0
    assert report.runs_with_cost == 0
    assert report.cost_per_successful_run_usd is None
    assert report.estimated_total_cost_usd == 0.20
    assert report.estimated_failure_tax_usd == 0.20
    assert report.estimated_failure_tax_ratio == 1.0
    assert report.runs_with_estimated_cost == 1
    assert report.estimated_cost_per_successful_run_usd is None
    assert report.by_workflow[0].estimated_failure_tax_usd == 0.20
    assert report.by_tenant[0].estimated_failure_tax_usd == 0.20
    assert "estimated" in report.note.lower()


def test_measured_and_estimated_spend_are_aggregated_in_separate_channels():
    report = unit_economics(
        [
            _run("s1", RunStatus.COMPLETED),
            _run("f-measured", RunStatus.FAILED),
            _run("f-estimated", RunStatus.FAILED),
        ],
        [
            _audit("s1", None, estimated_cost=0.10),
            _audit("f-measured", 0.20),
            _audit("f-estimated", None, estimated_cost=0.30),
        ],
    )

    assert report.total_cost_usd == 0.20
    assert report.terminal_cost_usd == 0.20
    assert report.cost_on_successful_usd == 0.0
    assert report.failure_tax_usd == 0.20
    assert report.runs_with_cost == 1
    assert report.estimated_total_cost_usd == 0.40
    assert report.estimated_terminal_cost_usd == 0.40
    assert report.estimated_cost_on_successful_usd == 0.10
    assert report.estimated_failure_tax_usd == 0.30
    assert report.runs_with_estimated_cost == 2
    assert report.estimated_cost_per_successful_run_usd == 0.40
    assert report.estimated_mean_cost_per_successful_run_usd == 0.10


def test_estimated_only_success_note_does_not_present_zero_as_the_true_cost():
    report = unit_economics(
        [_run("s1", RunStatus.COMPLETED)],
        [_audit("s1", None, estimated_cost=0.10)],
    )

    assert "estimated" in report.note.lower()
    assert "$0.1000" in report.note
    assert "costs ~$0.0000" not in report.note

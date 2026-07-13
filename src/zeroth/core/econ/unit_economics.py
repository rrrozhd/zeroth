"""Unit economics over the run trail -- cost per *successful* outcome (ECON-UNIT-01).

Cost metering answers "what did I spend"; right-sizing answers "could a model be
cheaper". Neither answers the question that decides whether an AI app is *economically
viable*: **what does one good outcome actually cost me** -- because you pay for the
failures too. This module joins the two facts the runtime already holds -- the
authoritative per-run outcome (``RunStatus`` on the :class:`~zeroth.core.runs.models.Run`)
and the per-run spend (summed ``NodeAuditRecord.cost_usd``) -- into that number.

An *outcome* is a **top-level run** (one end-to-end invocation). Sub-graph child runs
are persisted under their own ``deployment_ref`` and so never enter a deployment-scoped
query; a ``parent_run_id is None`` guard makes that explicit rather than incidental. Cost
is attributed exactly as :func:`~zeroth.core.econ.waste.analyze_run` does it -- the sum of
a run's own audit records' ``cost_usd`` -- so this surface never disagrees with the waste
report about what a run cost.

The honesty rules are the module's, carried over verbatim:

* The fully-loaded headline (``cost_per_successful_run_usd``) divides **terminal** spend
  (succeeded + failed) by successful runs -- it deliberately loads the failure tax onto
  each good result. In-flight spend is reported on its own line and never enters the
  metric, because a still-running run has not resolved to an outcome yet.
* ``cost_per_successful_run_usd`` is ``None`` when nothing has succeeded -- a zero
  denominator is never dressed up as a cheap outcome.
* When runs exist but no cost is attributed, the report says so and points at the likely
  cause (cost tracking disabled) rather than reporting a misleading ``$0`` viability.

Pure and in-process: no Regulus, no network -- feed it a window of runs and their audits.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.audit.models import NodeAuditRecord
from zeroth.core.econ.quality import QualityEconomicsReport, quality_economics
from zeroth.core.runs.models import Run, RunStatus

_SUCCESS = RunStatus.COMPLETED.value
_FAILED = RunStatus.FAILED.value


def _status(run: Run) -> str:
    """Normalize a run's status to its string value (enum or already-a-str)."""
    return getattr(run.status, "value", run.status)


def _bump(agg: list[float], status: str, run_cost: float) -> None:
    """Apply one run's contribution to a 6-slot group aggregate.

    Slots: [runs, successful, failed, terminal_cost, successful_cost, failed_cost].
    Shared by the by-workflow and by-tenant breakdowns so the two can't drift apart.
    """
    agg[0] += 1
    if status == _SUCCESS:
        agg[1] += 1
        agg[3] += run_cost
        agg[4] += run_cost
    elif status == _FAILED:
        agg[2] += 1
        agg[3] += run_cost
        agg[5] += run_cost


class WorkflowEconomics(BaseModel):
    """Unit economics for one workflow within a deployment."""

    model_config = ConfigDict(extra="forbid")

    workflow_name: str
    runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0.0
    terminal_cost_usd: float = 0.0
    # Fully-loaded: terminal spend / successful runs. None when nothing succeeded.
    cost_per_successful_run_usd: float | None = None
    # Spend on failed runs -- money that bought no usable outcome.
    failure_tax_usd: float = 0.0


class TenantEconomics(BaseModel):
    """Unit economics for one tenant (customer) within a deployment.

    The 'which customer is unprofitable' view: same shape as WorkflowEconomics, keyed
    by tenant_id instead of workflow_name.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0.0
    terminal_cost_usd: float = 0.0
    cost_per_successful_run_usd: float | None = None
    failure_tax_usd: float = 0.0


class UnitEconomicsReport(BaseModel):
    """Deployment-wide unit economics: what a successful outcome costs, and the failure tax.

    Computed over a bounded, most-recent window of top-level runs (``window_runs``); the
    dollar figures are for that window, not all time.
    """

    model_config = ConfigDict(extra="forbid")

    window_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    in_flight_runs: int = 0
    # successful / (successful + failed) -- in-flight runs are not yet outcomes.
    success_rate: float = 0.0

    # total = terminal (successful + failed) + in-flight, so the parts reconcile.
    total_cost_usd: float = 0.0
    terminal_cost_usd: float = 0.0
    cost_on_successful_usd: float = 0.0
    cost_on_failed_usd: float = 0.0
    cost_on_in_flight_usd: float = 0.0

    # Headline: fully-loaded cost of one good outcome (terminal spend / successes).
    cost_per_successful_run_usd: float | None = None
    # The "clean" cost of a success, excluding the failure tax (successful spend / successes).
    mean_cost_per_successful_run_usd: float | None = None
    # Spend on failed runs (== cost_on_failed_usd), surfaced as the headline "waste" number.
    failure_tax_usd: float = 0.0
    # Fraction of *terminal* spend that bought no usable outcome.
    failure_tax_ratio: float = 0.0

    # How many in-window runs had any attributed cost -- 0 while window_runs > 0 means
    # cost tracking is almost certainly off.
    runs_with_cost: int = 0

    by_workflow: list[WorkflowEconomics] = Field(default_factory=list)
    by_tenant: list[TenantEconomics] = Field(default_factory=list)
    # Optional quality-aware overlay: cost per *good* outcome when verdicts are attached.
    # Populated always (state='not_configured' when no verdicts) so a reader learns it exists.
    quality: QualityEconomicsReport | None = None
    note: str = ""


def unit_economics(
    runs: Sequence[Run],
    audits: Sequence[NodeAuditRecord],
    *,
    workflow_limit: int = 20,
    tenant_limit: int = 20,
) -> UnitEconomicsReport:
    """Join a window of top-level runs with their audit spend into unit economics.

    ``runs`` is the most-recent window for a deployment (already deployment-scoped by the
    caller); ``audits`` are that deployment's audit records. Cost is attributed only to
    runs in ``runs`` -- audit records for out-of-window runs are ignored so the window
    stays internally consistent. Sub-graph children are dropped (``parent_run_id``), so an
    outcome is always a top-level invocation.
    """
    # Per-run spend, exactly as waste.analyze_run attributes it: sum of the run's own
    # audit records' cost_usd (None -> 0).
    cost_by_run: dict[str, float] = {}
    for record in audits:
        cost_by_run[record.run_id] = cost_by_run.get(record.run_id, 0.0) + (record.cost_usd or 0.0)

    top_level = [r for r in runs if r.parent_run_id is None]

    successful = failed = in_flight = runs_with_cost = 0
    cost_ok = cost_bad = cost_flight = 0.0
    # key -> [runs, successful, failed, terminal_cost, successful_cost, failed_cost]
    wf: dict[str, list[float]] = {}
    tn: dict[str, list[float]] = {}

    for run in top_level:
        status = _status(run)
        run_cost = cost_by_run.get(run.run_id, 0.0)
        if run_cost > 0:
            runs_with_cost += 1

        _bump(wf.setdefault(run.workflow_name, [0.0] * 6), status, run_cost)
        _bump(tn.setdefault(run.tenant_id, [0.0] * 6), status, run_cost)

        if status == _SUCCESS:
            successful += 1
            cost_ok += run_cost
        elif status == _FAILED:
            failed += 1
            cost_bad += run_cost
        else:
            in_flight += 1
            cost_flight += run_cost

    terminal_runs = successful + failed
    terminal_cost = cost_ok + cost_bad
    total_cost = terminal_cost + cost_flight

    cost_per_success = round(terminal_cost / successful, 6) if successful else None
    mean_per_success = round(cost_ok / successful, 6) if successful else None
    success_rate = round(successful / terminal_runs, 4) if terminal_runs else 0.0
    failure_tax_ratio = round(cost_bad / terminal_cost, 4) if terminal_cost > 0 else 0.0

    by_workflow = [
        WorkflowEconomics(
            workflow_name=name,
            runs=int(a[0]),
            successful_runs=int(a[1]),
            failed_runs=int(a[2]),
            success_rate=round(a[1] / (a[1] + a[2]), 4) if (a[1] + a[2]) else 0.0,
            terminal_cost_usd=round(a[3], 6),
            cost_per_successful_run_usd=round(a[3] / a[1], 6) if a[1] else None,
            failure_tax_usd=round(a[5], 6),
        )
        for name, a in wf.items()
    ]
    by_workflow.sort(key=lambda w: w.terminal_cost_usd, reverse=True)
    by_workflow = by_workflow[:workflow_limit]

    by_tenant = [
        TenantEconomics(
            tenant_id=name,
            runs=int(a[0]),
            successful_runs=int(a[1]),
            failed_runs=int(a[2]),
            success_rate=round(a[1] / (a[1] + a[2]), 4) if (a[1] + a[2]) else 0.0,
            terminal_cost_usd=round(a[3], 6),
            cost_per_successful_run_usd=round(a[3] / a[1], 6) if a[1] else None,
            failure_tax_usd=round(a[5], 6),
        )
        for name, a in tn.items()
    ]
    by_tenant.sort(key=lambda t: t.terminal_cost_usd, reverse=True)
    by_tenant = by_tenant[:tenant_limit]

    report = UnitEconomicsReport(
        window_runs=len(top_level),
        successful_runs=successful,
        failed_runs=failed,
        in_flight_runs=in_flight,
        success_rate=success_rate,
        total_cost_usd=round(total_cost, 6),
        terminal_cost_usd=round(terminal_cost, 6),
        cost_on_successful_usd=round(cost_ok, 6),
        cost_on_failed_usd=round(cost_bad, 6),
        cost_on_in_flight_usd=round(cost_flight, 6),
        cost_per_successful_run_usd=cost_per_success,
        mean_cost_per_successful_run_usd=mean_per_success,
        failure_tax_usd=round(cost_bad, 6),
        failure_tax_ratio=failure_tax_ratio,
        runs_with_cost=runs_with_cost,
        by_workflow=by_workflow,
        by_tenant=by_tenant,
    )
    report.note = _note(report)
    report.quality = quality_economics(runs, audits)
    return report


def _note(report: UnitEconomicsReport) -> str:
    """Honest one-line reading of the report -- never oversell, never fake a number."""
    if report.window_runs == 0:
        return "No runs on record yet — invoke a workflow to build unit-economics history."
    if report.runs_with_cost == 0:
        return (
            f"The last {report.window_runs} run(s) are recorded but none has any attributed "
            "cost — they made no priced model calls (tool/code-only nodes, an unpriced model, "
            "or a run that failed before any spend)."
        )
    if report.successful_runs == 0:
        return (
            f"None of the last {report.window_runs} run(s) succeeded — "
            "cost per successful outcome is undefined until at least one succeeds."
        )
    tax = ""
    if report.failure_tax_usd > 0:
        tax = (
            f" ${report.failure_tax_usd:.4f} ({report.failure_tax_ratio:.0%} of terminal "
            "spend) was spent on failed runs."
        )
    return (
        f"{report.success_rate:.0%} of terminal runs succeeded; each successful outcome "
        f"costs ~${report.cost_per_successful_run_usd:.4f} fully loaded.{tax}"
    )

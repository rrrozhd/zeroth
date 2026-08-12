"""Economic waste detection over a run's audit trail (ECON-WASTE-01).

Zeroth is the runtime, so it sees the *graph* -- not just a flat stream of API
calls. This module turns the per-node cost already captured on
:class:`~zeroth.governance.audit.models.NodeAuditRecord` into a structured
:class:`EconReport` of :class:`WasteFinding` s, attributing spend to *structural*
causes (a failed run that still paid, a node re-executed in a loop). It is pure
and in-process: no Regulus, no network -- feed it a run's audit records.

Confirmed vs flagged -- the same honesty rule the eval harness applies to
errored-vs-failed. A *failed* run definitionally produced no usable output, so its
entire spend is **confirmed** waste. Loop re-execution is **flagged** for human
judgment, because a loop may be intentional refinement rather than a bug. The two
never share a dollar (see :func:`analyze_run`), so ``confirmed_waste_usd`` stays a
number you can put in front of someone without a caveat.

Detectors: ``paid_for_failed_run`` and ``loop_reexecution`` (P1); ``retry_overhead``
and ``cache_inefficiency`` (P2). Model right-sizing (cost x eval pass-rate) is a
cross-model shape that does not fit a per-run audit pass and lives outside
``analyze_run`` -- deferred to its own phase.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.governed import RunStatus
from zeroth.econ.measurement import MeasurementState
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.runtime.runs import Run


class WasteKind(StrEnum):
    """The category of an economic-waste finding."""

    PAID_FOR_FAILED_RUN = "paid_for_failed_run"
    LOOP_REEXECUTION = "loop_reexecution"
    RETRY_OVERHEAD = "retry_overhead"
    CACHE_INEFFICIENCY = "cache_inefficiency"


class WasteFinding(BaseModel):
    """One detected (or flagged) unit of economic waste in a run.

    ``confirmed`` separates epistemically-certain waste (a failed run produced no
    usable output) from spend ``flagged`` for review (recoverable only if it was
    unintended -- e.g. a loop that may be deliberate refinement).
    """

    model_config = ConfigDict(extra="forbid")

    kind: WasteKind
    node_id: str | None = None
    wasted_usd: float = 0.0
    confirmed: bool = False
    severity: str = "info"  # "info" | "warning" | "high"
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EconReport(BaseModel):
    """Per-run economic-waste report built from the run's audit records."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    run_status: str
    total_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    cost_measurement_complete: bool = True
    cost_by_node: dict[str, float] = Field(default_factory=dict)
    findings: list[WasteFinding] = Field(default_factory=list)

    @property
    def confirmed_waste_usd(self) -> float:
        """USD of epistemically-certain waste (e.g. all spend on a failed run)."""
        return sum(finding.wasted_usd for finding in self.findings if finding.confirmed)

    @property
    def flagged_waste_usd(self) -> float:
        """USD flagged for review -- recoverable if unintended (e.g. loop re-runs)."""
        return sum(finding.wasted_usd for finding in self.findings if not finding.confirmed)

    @property
    def waste_ratio(self) -> float:
        """Fraction of total spend that is confirmed or flagged as waste."""
        if self.total_cost_usd <= 0:
            return 0.0
        return (self.confirmed_waste_usd + self.flagged_waste_usd) / self.total_cost_usd

    def summary(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the report's headline metrics."""
        return {
            "run_id": self.run_id,
            "run_status": self.run_status,
            "total_cost_usd": self.total_cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_measurement_complete": self.cost_measurement_complete,
            "confirmed_waste_usd": self.confirmed_waste_usd,
            "flagged_waste_usd": self.flagged_waste_usd,
            "waste_ratio": self.waste_ratio,
            "findings": len(self.findings),
        }


EconReport.__signature__ = inspect.signature(EconReport).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(EconReport).parameters.items()
        if name not in {"estimated_cost_usd", "cost_measurement_complete"}
    ]
)


def _attempts(record: NodeAuditRecord) -> int:
    """Retry attempts for a node from its audit (defaults to 1).

    Reads the flat ``attempt`` the agent serializer promotes, because the nested
    ``extra`` section is content and does not survive the audit capture
    boundary's metadata-only default. The nested path stays as a fallback for
    records written under a content classification, and for rows already stored.
    """
    value = record.execution_metadata.get("attempt")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    extra = record.execution_metadata.get("extra")
    if isinstance(extra, Mapping):
        nested = extra.get("attempts")
        if isinstance(nested, int) and nested > 0:
            return nested
    return 1


def _cache_hit(record: NodeAuditRecord) -> bool | None:
    """Cache hit/miss for a node, or ``None`` when caching was not in the chain.

    Reads the flat ``disposition`` label the agent serializer promotes from the
    ``CachingProviderAdapter``'s flag, falling back to the nested
    ``response.metadata.cache_hit`` the flag is serialized under. Both paths are
    pinned by real-path tests so a serialization change cannot silently turn the
    detector inert (the lesson from the success-cost gap).
    """
    disposition = record.execution_metadata.get("disposition")
    if disposition == "cache_hit":
        return True
    if disposition == "cache_miss":
        return False
    response = record.execution_metadata.get("response")
    if isinstance(response, Mapping):
        metadata = response.get("metadata")
        if isinstance(metadata, Mapping) and "cache_hit" in metadata:
            return bool(metadata["cache_hit"])
    return None


def analyze_run(
    run_id: str,
    run_status: RunStatus,
    audits: Sequence[NodeAuditRecord],
) -> EconReport:
    """Build an :class:`EconReport` from a run's status and its audit records.

    Detectors (non-overlapping by construction, so dollars are never counted
    twice):

    * ``paid_for_failed_run`` (*confirmed*): a ``FAILED`` run that still incurred
      cost produced no usable output, so its entire spend is waste.
    * ``loop_reexecution`` (*flagged*): a node with more than one audit record
      re-spent on every execution but its most expensive one (so a repeat served
      from cache, which attributes $0, adds nothing). Counted as flagged waste
      only for non-failed runs; in a failed run that spend is already inside
      ``paid_for_failed_run`` and is surfaced as a zero-dollar info finding.

    Cost comes from ``NodeAuditRecord.cost_usd`` (``None`` is treated as ``0``),
    which is populated for instrumented LLM calls -- including calls that were
    paid for and then failed validation (see the runner's cost-on-failure path).
    """
    cost_by_node: dict[str, float] = {}
    exec_costs: dict[str, list[float]] = {}
    total_cost = 0.0
    estimated_cost = 0.0
    measurement_complete = True
    for record in audits:
        if record.cost_measurement is MeasurementState.UNMEASURED:
            measurement_complete = False
        estimated_cost += (
            record.estimated_cost_usd if record.estimated_cost_usd is not None else 0.0
        )
        cost = record.cost_usd if record.cost_usd is not None else 0.0
        total_cost += cost
        cost_by_node[record.node_id] = cost_by_node.get(record.node_id, 0.0) + cost
        exec_costs.setdefault(record.node_id, []).append(cost)

    failed = run_status == RunStatus.FAILED
    findings: list[WasteFinding] = []

    if failed and total_cost > 0:
        findings.append(
            WasteFinding(
                kind=WasteKind.PAID_FOR_FAILED_RUN,
                wasted_usd=total_cost,
                confirmed=True,
                severity="high",
                detail=f"run failed after spending ${total_cost:.4f}; no usable output produced",
            )
        )

    for node_id, costs in exec_costs.items():
        executions = len(costs)
        node_cost = sum(costs)
        if executions <= 1 or node_cost <= 0:
            continue
        # Recoverable re-execution waste = total node spend minus the single
        # most-expensive execution (the one you keep). Summing all-but-the-max
        # rather than averaging node_cost across visits means a repeat that cost
        # $0 -- a cache hit attributes zero (see econ.adapter) -- adds nothing.
        # Averaging would smear the one paid execution over every visit and
        # over-report what is actually recoverable.
        repeat_cost = node_cost - max(costs)
        if failed:
            findings.append(
                WasteFinding(
                    kind=WasteKind.LOOP_REEXECUTION,
                    node_id=node_id,
                    wasted_usd=0.0,
                    confirmed=False,
                    severity="info",
                    detail=(
                        f"node executed {executions}x "
                        "(cost already counted in the failed-run total)"
                    ),
                    metadata={"executions": executions, "node_cost_usd": node_cost},
                )
            )
        elif repeat_cost > 0:
            findings.append(
                WasteFinding(
                    kind=WasteKind.LOOP_REEXECUTION,
                    node_id=node_id,
                    wasted_usd=repeat_cost,
                    confirmed=False,
                    severity="warning",
                    detail=(
                        f"node executed {executions}x for ${node_cost:.4f}; "
                        f"~${repeat_cost:.4f} is re-execution (recoverable if unintended)"
                    ),
                    metadata={"executions": executions, "node_cost_usd": node_cost},
                )
            )
        else:
            # Every repeat cost $0 (e.g. served from cache): the loop multiplicity
            # is real and worth surfacing, but nothing is recoverable -- info, not
            # a warning, so a $0 finding never reads as recoverable money.
            findings.append(
                WasteFinding(
                    kind=WasteKind.LOOP_REEXECUTION,
                    node_id=node_id,
                    wasted_usd=0.0,
                    confirmed=False,
                    severity="info",
                    detail=(
                        f"node executed {executions}x for ${node_cost:.4f}; "
                        "repeated executions added no cost (e.g. served from cache)"
                    ),
                    metadata={"executions": executions, "node_cost_usd": node_cost},
                )
            )

    for record in audits:
        attempts = _attempts(record)
        cost = record.cost_usd if record.cost_usd is not None else 0.0
        if attempts <= 1 or cost <= 0:
            continue
        # Failed attempts aren't recorded individually; estimate their cost as the
        # final (recorded) attempt's. Different dollars from loop_reexecution
        # (retries within one record vs repeated records), so no double-count.
        estimated = cost * (attempts - 1)
        if failed:
            findings.append(
                WasteFinding(
                    kind=WasteKind.RETRY_OVERHEAD,
                    node_id=record.node_id,
                    wasted_usd=0.0,
                    severity="info",
                    detail=(
                        f"node succeeded after {attempts} attempts "
                        "(cost already counted in the failed-run total)"
                    ),
                    metadata={"attempts": attempts},
                )
            )
        else:
            findings.append(
                WasteFinding(
                    kind=WasteKind.RETRY_OVERHEAD,
                    node_id=record.node_id,
                    wasted_usd=estimated,
                    severity="warning",
                    detail=(
                        f"node succeeded after {attempts} attempts; "
                        f"~${estimated:.4f} estimated retry overhead"
                    ),
                    metadata={"attempts": attempts, "final_cost_usd": cost},
                )
            )

    # Cache efficiency -- info only, and only when caching is actually in the chain
    # (otherwise every uncached run would be stamped "0% hit rate" -- pure noise).
    cache_flags = [hit for hit in (_cache_hit(record) for record in audits) if hit is not None]
    if cache_flags:
        hits = sum(1 for hit in cache_flags if hit)
        total = len(cache_flags)
        findings.append(
            WasteFinding(
                kind=WasteKind.CACHE_INEFFICIENCY,
                wasted_usd=0.0,
                severity="info",
                detail=(
                    f"cache hit rate {hits / total:.0%} ({hits}/{total}); {total - hits} miss(es)"
                ),
                metadata={"hits": hits, "misses": total - hits, "hit_rate": hits / total},
            )
        )

    return EconReport(
        run_id=run_id,
        run_status=getattr(run_status, "value", str(run_status)),
        total_cost_usd=total_cost,
        estimated_cost_usd=estimated_cost,
        cost_measurement_complete=measurement_complete,
        cost_by_node=cost_by_node,
        findings=findings,
    )


class EconThresholdError(Exception):
    """Raised by :func:`waste_gate` when a report exceeds its configured limits."""


def waste_gate(
    report: EconReport,
    *,
    max_confirmed_usd: float | None = None,
    max_flagged_usd: float | None = None,
    max_waste_ratio: float | None = None,
) -> None:
    """Raise :class:`EconThresholdError` if the report exceeds any given limit.

    The economic analog of the eval harness's quality ``gate``: lets a pipeline
    fail when a run burns too much money on waste. Thresholds are opt-in -- an
    unset limit is never enforced.
    """
    problems: list[str] = []
    if not report.cost_measurement_complete and any(
        limit is not None for limit in (max_confirmed_usd, max_flagged_usd, max_waste_ratio)
    ):
        problems.append("cost measurement is incomplete; waste thresholds cannot be decided")
    if max_confirmed_usd is not None and report.confirmed_waste_usd > max_confirmed_usd:
        problems.append(
            f"confirmed_waste ${report.confirmed_waste_usd:.4f} > max ${max_confirmed_usd:.4f}"
        )
    if max_flagged_usd is not None and report.flagged_waste_usd > max_flagged_usd:
        problems.append(
            f"flagged_waste ${report.flagged_waste_usd:.4f} > max ${max_flagged_usd:.4f}"
        )
    if max_waste_ratio is not None and report.waste_ratio > max_waste_ratio:
        problems.append(f"waste_ratio {report.waste_ratio:.1%} > max {max_waste_ratio:.1%}")
    if problems:
        raise EconThresholdError("; ".join(problems))


# ---------------------------------------------------------------------------
# Deployment-wide rollup (ECON-WASTE-02) -- the API/console surface.
#
# analyze_run is per-run; this rolls it up over a window of top-level runs so the
# waste report has a home on the Cost page next to unit economics. Confirmed and
# flagged never share a dollar (analyze_run guarantees it), so the summed totals
# stay as honest as the per-run buckets.
# ---------------------------------------------------------------------------


class WasteRollupFinding(WasteFinding):
    """A waste finding tagged with the run it came from, so it stays actionable."""

    run_id: str


class WasteKindTotal(BaseModel):
    """Per-kind waste totals across the window."""

    model_config = ConfigDict(extra="forbid")

    kind: WasteKind
    count: int = 0
    wasted_usd: float = 0.0


class WasteRollup(BaseModel):
    """Deployment-wide economic-waste rollup over a window of runs."""

    model_config = ConfigDict(extra="forbid")

    window_runs: int = 0
    runs_with_waste: int = 0
    runs_with_cost: int = 0
    total_cost_usd: float = 0.0
    total_confirmed_waste_usd: float = 0.0
    total_flagged_waste_usd: float = 0.0
    # (confirmed + flagged) / total spend across the window -- spend-weighted, not a
    # mean of per-run ratios; 0.0 when nothing was spent.
    waste_ratio: float = 0.0
    findings: int = 0
    confirmed_findings: int = 0
    flagged_findings: int = 0
    by_kind: list[WasteKindTotal] = Field(default_factory=list)
    top_findings: list[WasteRollupFinding] = Field(default_factory=list)
    note: str = ""


def waste_rollup(
    runs: Sequence[Run],
    audits: Sequence[NodeAuditRecord],
    *,
    top_n: int = 10,
) -> WasteRollup:
    """Aggregate per-run waste over a window of top-level runs into a deployment rollup.

    Calls :func:`analyze_run` per top-level run (grouping that run's audits, passing its
    status) and sums the confirmed/flagged buckets. ``run.status`` is passed straight
    through -- :func:`analyze_run` compares it against ``RunStatus.FAILED`` and reads its
    ``.value`` defensively, so an enum or a bare string both work (no coercion that could
    raise on an unexpected value). Sub-graph children are dropped (``parent_run_id``).
    """
    audits_by_run: dict[str, list[NodeAuditRecord]] = {}
    for record in audits:
        audits_by_run.setdefault(record.run_id, []).append(record)

    top_level = [r for r in runs if r.parent_run_id is None]
    # Waste is a property of finished runs -- an in-flight run's spend hasn't resolved to
    # an outcome yet, so (consistent with unit economics) analyze only terminal runs.
    terminal_statuses = {RunStatus.COMPLETED.value, RunStatus.FAILED.value}
    terminal = [r for r in top_level if getattr(r.status, "value", r.status) in terminal_statuses]

    total_cost = confirmed = flagged = 0.0
    runs_with_cost = runs_with_waste = 0
    n_findings = n_confirmed = n_flagged = 0
    by_kind: dict[WasteKind, list[float]] = {}  # kind -> [count, wasted]
    ranked: list[WasteRollupFinding] = []

    for run in terminal:
        report = analyze_run(run.run_id, run.status, audits_by_run.get(run.run_id, []))
        total_cost += report.total_cost_usd
        if report.total_cost_usd > 0:
            runs_with_cost += 1
        c, f = report.confirmed_waste_usd, report.flagged_waste_usd
        confirmed += c
        flagged += f
        if c + f > 0:
            runs_with_waste += 1
        for finding in report.findings:
            n_findings += 1
            if finding.confirmed:
                n_confirmed += 1
            else:
                n_flagged += 1
            slot = by_kind.setdefault(finding.kind, [0.0, 0.0])
            slot[0] += 1
            slot[1] += finding.wasted_usd
            # Only surface findings that name recoverable money -- a $0 info finding
            # (cache hit-rate, cache-served loop) never reads as reclaimable.
            if finding.wasted_usd > 0:
                ranked.append(WasteRollupFinding(run_id=run.run_id, **finding.model_dump()))

    ranked.sort(key=lambda x: x.wasted_usd, reverse=True)
    waste_ratio = round((confirmed + flagged) / total_cost, 4) if total_cost > 0 else 0.0

    rollup = WasteRollup(
        window_runs=len(terminal),
        runs_with_waste=runs_with_waste,
        runs_with_cost=runs_with_cost,
        total_cost_usd=round(total_cost, 6),
        total_confirmed_waste_usd=round(confirmed, 6),
        total_flagged_waste_usd=round(flagged, 6),
        waste_ratio=waste_ratio,
        findings=n_findings,
        confirmed_findings=n_confirmed,
        flagged_findings=n_flagged,
        by_kind=[
            WasteKindTotal(kind=k, count=int(v[0]), wasted_usd=round(v[1], 6))
            for k, v in by_kind.items()
        ],
        top_findings=ranked[:top_n],
    )
    rollup.note = _rollup_note(rollup)
    return rollup


def _rollup_note(report: WasteRollup) -> str:
    """Honest one-line reading of the rollup -- never overclaim, never fake a number."""
    if report.window_runs == 0:
        return "No runs on record yet — invoke a workflow to build a waste history."
    if report.runs_with_cost == 0:
        return (
            f"The last {report.window_runs} run(s) are recorded but none has attributed cost — "
            "no priced model spend to analyse for waste yet."
        )
    if report.total_confirmed_waste_usd + report.total_flagged_waste_usd == 0:
        return f"No economic waste detected across the last {report.window_runs} run(s)."
    where = ""
    if report.top_findings:
        top = report.top_findings[0]
        where = f"; largest: {top.kind} on {top.node_id or top.run_id} at ${top.wasted_usd:.4f}"
    return (
        f"${report.total_confirmed_waste_usd:.4f} confirmed + "
        f"${report.total_flagged_waste_usd:.4f} flagged waste across {report.window_runs} run(s) "
        f"({report.waste_ratio:.0%} of ${report.total_cost_usd:.4f} spend){where}."
    )

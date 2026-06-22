"""Economic waste detection over a run's audit trail (ECON-WASTE-01).

Zeroth is the runtime, so it sees the *graph* -- not just a flat stream of API
calls. This module turns the per-node cost already captured on
:class:`~zeroth.core.audit.models.NodeAuditRecord` into a structured
:class:`EconReport` of :class:`WasteFinding` s, attributing spend to *structural*
causes (a failed run that still paid, a node re-executed in a loop). It is pure
and in-process: no Regulus, no network -- feed it a run's audit records.

Confirmed vs flagged -- the same honesty rule the eval harness applies to
errored-vs-failed. A *failed* run definitionally produced no usable output, so its
entire spend is **confirmed** waste. Loop re-execution is **flagged** for human
judgment, because a loop may be intentional refinement rather than a bug. The two
never share a dollar (see :func:`analyze_run`), so ``confirmed_waste_usd`` stays a
number you can put in front of someone without a caveat.

Scope (P1): the ``paid_for_failed_run`` and ``loop_reexecution`` detectors. Cache
inefficiency, retry/fallback overhead, and model right-sizing (cost x eval) are
deferred to a later phase -- their signals exist on the audit record but are not
yet first-class fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.audit.models import NodeAuditRecord
from zeroth.core.runs import RunStatus


class WasteKind(StrEnum):
    """The category of an economic-waste finding."""

    PAID_FOR_FAILED_RUN = "paid_for_failed_run"
    LOOP_REEXECUTION = "loop_reexecution"


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
            "confirmed_waste_usd": self.confirmed_waste_usd,
            "flagged_waste_usd": self.flagged_waste_usd,
            "waste_ratio": self.waste_ratio,
            "findings": len(self.findings),
        }


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
      re-spent on all but one execution. Counted as flagged waste only for
      non-failed runs; in a failed run that spend is already inside
      ``paid_for_failed_run`` and is surfaced as a zero-dollar info finding.

    Cost comes from ``NodeAuditRecord.cost_usd`` (``None`` is treated as ``0``),
    which is populated for instrumented LLM calls -- including calls that were
    paid for and then failed validation (see the runner's cost-on-failure path).
    """
    cost_by_node: dict[str, float] = {}
    exec_costs: dict[str, list[float]] = {}
    total_cost = 0.0
    for record in audits:
        cost = record.cost_usd or 0.0
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
        repeat_cost = node_cost - node_cost / executions
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
        else:
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

    return EconReport(
        run_id=run_id,
        run_status=getattr(run_status, "value", str(run_status)),
        total_cost_usd=total_cost,
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

"""Quality-aware outcomes overlay (ECON-QUALITY-01).

Unit economics counts a run as a 'success' when it COMPLETED. But a completed run can
still return a bad answer, so 'cost per successful run' can understate the true cost of a
*good* outcome. This module overlays an OPTIONAL quality signal: when a run carries an
externally-attached verdict (good/bad) in its metadata, it reports cost-per-*quality*-
success over the LABELED subset only.

Honesty is the whole point -- the runtime does not automatically know whether an answer was
good, so this never invents one:

* No verdict / malformed verdict -> ``unknown`` -> the run is excluded from BOTH the
  numerator and the denominator. It is never silently counted as good, never as a failure.
* The headline ``cost_per_quality_success`` divides the LABELED terminal spend by the GOOD
  labeled runs -- both drawn from the same labeled population, so a thin sample is never
  divided into full-window dollars.
* Below a coverage floor (or with zero labels) the number is ``None`` with an explicit
  ``state``, never a footnoted figure dressed up as a live metric.

Verdicts are attached out-of-band -- ``POST /econ/quality-verdict``, or any producer that
writes ``run.metadata['quality_verdict']`` (a human reviewer, a downstream check, or a
future inline scorer node). The offline eval harness is deliberately NOT a producer: its
synthetic cases have no production ``run_id`` to attach to.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.audit.models import NodeAuditRecord
from zeroth.core.runs.models import Run, RunStatus

_SUCCESS = RunStatus.COMPLETED.value
_FAILED = RunStatus.FAILED.value
_TERMINAL = {_SUCCESS, _FAILED}

QualityLabel = Literal["good", "bad", "unknown"]


class RunQualityVerdict(BaseModel):
    """An externally-attached judgement of whether a run's output was good."""

    model_config = ConfigDict(extra="forbid")

    verdict: QualityLabel
    score: float | None = None
    source: str
    rubric_id: str | None = None
    detail: str = ""
    # The human-provided *correct* answer for this run, when the reviewer supplies one. This
    # is what turns a good/bad judgement into a labeled eval case: correctness-graded
    # right-sizing measures a candidate model against this, not against the incumbent's output.
    expected_output: str | None = None
    attached_at: datetime | None = None


class QualityEconomicsReport(BaseModel):
    """Cost per *quality* success over the labeled subset of the window.

    Every headline travels with its coverage so the number can never be read alone.
    """

    model_config = ConfigDict(extra="forbid")

    terminal_runs: int = 0
    labeled_terminal_runs: int = 0
    coverage: float = 0.0  # labeled / terminal
    quality_successes: int = 0  # completed AND judged good
    quality_success_rate_over_labeled: float = 0.0
    cost_per_quality_success_usd: float | None = None
    # Labeled spend on completed-but-bad and labeled-failed runs -- money that bought no
    # *good* outcome, measured within the labeled subset only.
    cost_on_quality_failures_usd: float = 0.0
    sources: list[str] = Field(default_factory=list)
    state: Literal["ok", "not_configured", "below_coverage_floor"] = "not_configured"
    note: str = ""


def read_quality_verdict(run: Run) -> RunQualityVerdict | None:
    """Parse a run's attached quality verdict, or ``None`` if absent/malformed.

    Never raises and never defaults to ``good`` -- a malformed blob is treated as no verdict.
    """
    metadata = getattr(run, "metadata", None)
    raw = metadata.get("quality_verdict") if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return RunQualityVerdict.model_validate(raw)
    except Exception:
        return None


def _status(run: Run) -> str:
    return getattr(run.status, "value", run.status)


def quality_economics(
    runs: Sequence[Run],
    audits: Sequence[NodeAuditRecord],
    *,
    min_coverage: float = 0.2,
) -> QualityEconomicsReport:
    """Cost per quality success over the labeled terminal subset of top-level runs.

    Only runs with a good/bad verdict enter the metric; unknown/unlabeled runs are excluded
    from both numerator and denominator. ``cost_per_quality_success`` divides the labeled
    terminal spend by the good labeled runs. Below ``min_coverage`` (or with zero labels)
    the headline is ``None`` with an explanatory ``state``.
    """
    cost_by_run: dict[str, float] = {}
    for record in audits:
        cost_by_run[record.run_id] = cost_by_run.get(record.run_id, 0.0) + (record.cost_usd or 0.0)

    top_level = [r for r in runs if r.parent_run_id is None]
    terminal = [r for r in top_level if _status(r) in _TERMINAL]

    labeled_cost = bad_cost = 0.0
    good = bad = 0
    sources: set[str] = set()

    for run in terminal:
        verdict = read_quality_verdict(run)
        if verdict is None or verdict.verdict == "unknown":
            continue
        run_cost = cost_by_run.get(run.run_id, 0.0)
        labeled_cost += run_cost
        sources.add(verdict.source)
        # A quality success = completed AND judged good. Everything else labeled
        # (completed-but-bad, or a failed run someone labeled) is a quality failure.
        # cost_per_quality_success loads the full labeled spend onto the good runs, the
        # same way cost_per_successful_run loads the failure tax onto each success.
        if _status(run) == _SUCCESS and verdict.verdict == "good":
            good += 1
        else:
            bad += 1
            bad_cost += run_cost

    labeled = good + bad
    coverage = round(labeled / len(terminal), 4) if terminal else 0.0

    if labeled == 0:
        state: Literal["ok", "not_configured", "below_coverage_floor"] = "not_configured"
        cps: float | None = None
    elif coverage < min_coverage:
        state = "below_coverage_floor"
        cps = None
    else:
        state = "ok"
        cps = round(labeled_cost / good, 6) if good else None

    report = QualityEconomicsReport(
        terminal_runs=len(terminal),
        labeled_terminal_runs=labeled,
        coverage=coverage,
        quality_successes=good,
        quality_success_rate_over_labeled=round(good / labeled, 4) if labeled else 0.0,
        cost_per_quality_success_usd=cps,
        cost_on_quality_failures_usd=round(bad_cost, 6),
        sources=sorted(sources),
        state=state,
    )
    report.note = _quality_note(report, min_coverage)
    return report


def _quality_note(report: QualityEconomicsReport, min_coverage: float) -> str:
    """Honest one-line reading -- never fake a quality number the runtime can't know."""
    if report.state == "not_configured":
        return (
            "No quality verdicts attached — 'success' still means a completed run. Attach "
            "verdicts (POST /econ/quality-verdict) to measure cost per *good* outcome."
        )
    if report.state == "below_coverage_floor":
        return (
            f"Only {report.coverage:.0%} of terminal runs are labeled "
            f"(need ≥{min_coverage:.0%}) — too few to report cost per quality success."
        )
    if report.cost_per_quality_success_usd is None:
        return (
            f"{report.labeled_terminal_runs} run(s) labeled but none judged good — "
            "cost per quality success is undefined until at least one is."
        )
    return (
        f"Over {report.labeled_terminal_runs} labeled run(s) ({report.coverage:.0%} coverage), "
        f"each good outcome costs ~${report.cost_per_quality_success_usd:.4f}; "
        f"{report.quality_success_rate_over_labeled:.0%} of labeled runs were good."
    )

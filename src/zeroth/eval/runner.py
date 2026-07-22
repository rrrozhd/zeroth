"""Evaluation runner and CI gate (EVAL-04).

``run_eval`` runs each case's input through a ``target`` callable, scores the output
with the configured scorers, and returns an :class:`EvalReport`. ``gate`` turns a
report into a CI pass/fail by raising :class:`EvalThresholdError` below an absolute
pass-rate floor.

Scope: ``gate`` is an *absolute* threshold gate. Baseline-diff regression detection
(comparing against a stored prior run) is deferred — the building block here is the
report + absolute gate; wiring a baseline into CI is the caller's to add.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from zeroth.eval.models import CaseResult, EvalDataset, EvalReport
from zeroth.eval.scorers import Scorer

# A target maps a case input to an output, sync or async. Wrap an agent with
# ``async def target(inp): return (await runner.run(inp)).output_data``.
EvalTarget = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class EvalThresholdError(Exception):
    """Raised by :func:`gate` when a report falls below its configured thresholds."""


async def run_eval(
    dataset: EvalDataset,
    target: EvalTarget,
    scorers: Sequence[Scorer],
) -> EvalReport:
    """Run ``dataset`` through ``target`` and score each output.

    ``target`` maps a case input to an output (sync or async). A target failure is
    recorded as ``CaseResult.error`` (the case counts as not passed) rather than
    aborting the run, so one bad case cannot hide the rest of the results.
    """
    scorer_list = list(scorers)
    if not scorer_list:
        raise ValueError("run_eval requires at least one scorer")
    results: list[CaseResult] = []
    for case in dataset.cases:
        try:
            output = target(case.input)
            if inspect.isawaitable(output):
                output = await output
        except Exception as exc:
            results.append(CaseResult(case_id=case.id, error=f"target failed: {exc}"))
            continue
        scores = [await scorer.score(output, case) for scorer in scorer_list]
        results.append(CaseResult(case_id=case.id, output=output, scores=scores))
    return EvalReport(dataset=dataset.name, results=results)


def gate(
    report: EvalReport,
    *,
    min_pass_rate: float,
    max_error_rate: float | None = None,
) -> None:
    """Raise :class:`EvalThresholdError` if the report misses its thresholds.

    Enforces an absolute pass-rate floor (and an optional error-rate ceiling). The
    errored-case count is always included in the message so a low pass rate driven
    by harness/target failures is never silent.
    """
    problems: list[str] = []
    if report.pass_rate < min_pass_rate:
        problems.append(f"pass_rate {report.pass_rate:.1%} < min {min_pass_rate:.1%}")
    if max_error_rate is not None and report.error_rate > max_error_rate:
        problems.append(f"error_rate {report.error_rate:.1%} > max {max_error_rate:.1%}")
    if problems:
        raise EvalThresholdError(
            f"{'; '.join(problems)} ({report.errored_count}/{report.total} cases errored)"
        )

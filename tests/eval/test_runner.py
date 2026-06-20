"""Tests for the eval runner and CI gate (EVAL-04)."""

from __future__ import annotations

import pytest

from zeroth.core.eval import (
    CaseResult,
    EvalDataset,
    EvalReport,
    EvalThresholdError,
    ExactMatchScorer,
    Score,
    gate,
    run_eval,
)


def _dataset() -> EvalDataset:
    return EvalDataset.from_records(
        "d",
        [
            {"id": "1", "input": {"x": 1}, "expected": "a"},
            {"id": "2", "input": {"x": 2}, "expected": "b"},
        ],
    )


@pytest.mark.asyncio
async def test_run_eval_scores_each_case() -> None:
    async def target(inp):  # noqa: ANN001
        return "a" if inp["x"] == 1 else "z"

    report = await run_eval(_dataset(), target, [ExactMatchScorer()])
    assert report.total == 2
    assert report.passed_count == 1  # case 1 -> "a" matches; case 2 -> "z" != "b"


@pytest.mark.asyncio
async def test_run_eval_accepts_a_sync_target() -> None:
    def target(inp):  # noqa: ANN001 - sync target supported too
        return "a" if inp["x"] == 1 else "b"

    report = await run_eval(_dataset(), target, [ExactMatchScorer()])
    assert report.passed_count == 2


@pytest.mark.asyncio
async def test_run_eval_records_target_failure_as_errored() -> None:
    async def target(inp):  # noqa: ANN001
        raise RuntimeError("boom")

    report = await run_eval(_dataset(), target, [ExactMatchScorer()])
    assert report.errored_count == 2
    assert report.pass_rate == 0.0
    assert all(r.error is not None for r in report.results)


@pytest.mark.asyncio
async def test_run_eval_requires_a_scorer() -> None:
    with pytest.raises(ValueError, match="at least one scorer"):
        await run_eval(_dataset(), lambda inp: None, [])


def _report(pass_rate_numerator: int, total: int, errored: int = 0) -> EvalReport:
    results: list[CaseResult] = []
    for i in range(pass_rate_numerator):
        results.append(
            CaseResult(case_id=f"p{i}", output={}, scores=[Score(scorer="s", passed=True)])
        )
    for i in range(errored):
        results.append(CaseResult(case_id=f"e{i}", error="boom"))
    for i in range(total - pass_rate_numerator - errored):
        results.append(
            CaseResult(case_id=f"f{i}", output={}, scores=[Score(scorer="s", passed=False)])
        )
    return EvalReport(dataset="d", results=results)


def test_gate_passes_above_threshold() -> None:
    gate(_report(8, 10), min_pass_rate=0.75)  # 80% >= 75%, no raise


def test_gate_raises_below_threshold_with_error_count_in_message() -> None:
    report = _report(2, 10, errored=3)  # 20% pass, 30% errored
    with pytest.raises(EvalThresholdError, match=r"pass_rate.*3/10 cases errored"):
        gate(report, min_pass_rate=0.8)


def test_gate_enforces_error_rate_ceiling() -> None:
    report = _report(9, 10, errored=1)  # 90% pass but 10% errored
    with pytest.raises(EvalThresholdError, match="error_rate"):
        gate(report, min_pass_rate=0.0, max_error_rate=0.05)

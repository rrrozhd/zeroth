"""Tests for eval dataset loading and report aggregation (EVAL-01, EVAL-04)."""

from __future__ import annotations

from zeroth.core.eval import CaseResult, EvalDataset, EvalReport, Score


def test_dataset_from_jsonl_and_records() -> None:
    text = '{"id": "1", "input": {"q": "hi"}, "expected": "yo"}\n\n{"id": "2", "input": {}}'
    dataset = EvalDataset.from_jsonl("demo", text)
    assert [c.id for c in dataset.cases] == ["1", "2"]
    assert dataset.cases[0].expected == "yo"
    assert dataset.cases[1].expected is None  # blank lines skipped, defaults applied


def _report_with_mixed_outcomes() -> EvalReport:
    return EvalReport(
        dataset="d",
        results=[
            CaseResult(case_id="ok", output={}, scores=[Score(scorer="s", value=1.0, passed=True)]),
            CaseResult(
                case_id="fail", output={}, scores=[Score(scorer="s", value=0.0, passed=False)]
            ),
            CaseResult(case_id="target_err", error="boom"),  # target failed
            CaseResult(
                case_id="judge_err",
                output={},
                scores=[Score(scorer="s", passed=False, error="judge down")],  # scorer errored
            ),
        ],
    )


def test_errored_cases_count_against_pass_rate_and_are_surfaced() -> None:
    report = _report_with_mixed_outcomes()
    assert report.total == 4
    assert report.passed_count == 1
    # both the target failure and the judge error count as errored...
    assert report.errored_count == 2
    # ...and against the pass rate (errored cases stay in the denominator)
    assert report.pass_rate == 0.25
    assert report.error_rate == 0.5


def test_mean_scores_excludes_errored_scores() -> None:
    # mean over measurable scores only: ok(1.0) + fail(0.0) = 0.5; judge_err excluded
    assert _report_with_mixed_outcomes().mean_scores() == {"s": 0.5}


def test_case_passed_requires_scores_and_no_errors() -> None:
    assert CaseResult(case_id="a", output={}, error="x").passed is False
    assert CaseResult(case_id="b", output={}).passed is False  # no scores
    good = CaseResult(case_id="c", output={}, scores=[Score(scorer="s", passed=True)])
    assert good.passed is True

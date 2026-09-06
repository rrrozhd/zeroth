"""Quality contracts apply independently of the transport/executor path."""

from datetime import UTC, datetime

import pytest

from zeroth.econ.plane.backtesting.schemas import BacktestComputation, BacktestCreate
from zeroth.econ.plane.backtesting.service import decide, evidence_gaps


def _request(minimum):
    return BacktestCreate(
        workflow="quality-contract", baseline_version="v1", node_id="reply",
        incumbent_model="openai/incumbent", instruction="Answer the case.",
        candidate={"model": "openai/candidate"},
        cases=[{"id": str(i), "input": {"n": i}, "expected": {"n": i}} for i in range(5)],
        constraints={} if minimum is None else {"min_success_rate": minimum},
    )


@pytest.mark.parametrize("quality", [0, 0.5, 1])
@pytest.mark.parametrize("savings", [-10, 0, 30])
def test_missing_quality_requirement_cannot_authorize_any_computation(quality, savings) -> None:
    request = _request(None)
    report = decide(
        request,
        BacktestComputation(candidate_success_rate=quality, savings_pct=savings),
        digest="fixture", evaluated_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert "constraints.min_success_rate" in evidence_gaps(request)
    assert report.verdict == "abstain"
    assert report.recommended_action == "collect_evidence"
    assert "constraints.min_success_rate" in report.reasons


@pytest.mark.parametrize(
    "minimum, quality, savings, verdict",
    [(0, 0, 30, "pass"), (0.8, 0, 30, "fail"), (0.8, 0.8, 30, "pass"),
     (0.8, 1, 0, "fail"), (0.8, 1, -10, "fail")],
)
def test_explicit_quality_and_savings_contracts_keep_their_boundaries(minimum, quality, savings, verdict):
    report = decide(
        _request(minimum),
        BacktestComputation(candidate_success_rate=quality, savings_pct=savings),
        digest="fixture", evaluated_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert report.verdict == verdict

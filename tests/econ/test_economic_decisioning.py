from __future__ import annotations

from decimal import Decimal

from zeroth.econ.decisioning import (
    DecisionPolicy,
    RunEvidence,
    VersionEvidence,
    compare_workflow_versions,
)
from zeroth.econ.measurement import MeasurementState


def _run(
    run_id: str,
    *,
    cost: str | None,
    accepted: bool | None,
    measurement: MeasurementState = MeasurementState.MEASURED,
    outcome_measurement: MeasurementState = MeasurementState.MEASURED,
) -> RunEvidence:
    return RunEvidence(
        run_id=run_id,
        cost_usd=Decimal(cost) if cost is not None else None,
        cost_measurement=measurement,
        accepted=accepted,
        outcome_measurement=outcome_measurement,
    )


def _version(version: str, runs: list[RunEvidence]) -> VersionEvidence:
    return VersionEvidence(workflow="invoice-agent", version=version, runs=runs)


def test_comparison_abstains_when_candidate_outcome_coverage_is_too_low() -> None:
    baseline = _version(
        "v1",
        [_run(f"b-{index}", cost="1", accepted=True) for index in range(10)],
    )
    candidate = _version(
        "v2",
        [
            *[_run(f"c-{index}", cost="0.50", accepted=True) for index in range(5)],
            *[_run(f"c-{index}", cost="0.50", accepted=None) for index in range(5, 10)],
        ],
    )

    report = compare_workflow_versions(
        baseline,
        candidate,
        policy=DecisionPolicy(min_runs=10, min_outcome_coverage=0.8),
    )

    assert report.verdict == "abstain"
    assert report.recommended_action == "collect_evidence"
    assert report.reason_codes == ["candidate_outcome_coverage_below_minimum"]
    assert report.candidate.outcome_coverage == 0.5
    assert report.candidate.cost_per_accepted_outcome_usd is None


def test_comparison_holds_candidate_that_breaks_success_constraint() -> None:
    baseline = _version(
        "v1",
        [
            *[_run(f"b-{index}", cost="1", accepted=True) for index in range(9)],
            _run("b-9", cost="1", accepted=False),
        ],
    )
    candidate = _version(
        "v2",
        [
            *[_run(f"c-{index}", cost="0.50", accepted=True) for index in range(7)],
            *[_run(f"c-{index}", cost="0.50", accepted=False) for index in range(7, 10)],
        ],
    )

    report = compare_workflow_versions(
        baseline,
        candidate,
        policy=DecisionPolicy(
            min_runs=10,
            min_outcome_coverage=1,
            min_success_rate=0.85,
            max_success_rate_drop=0.02,
        ),
    )

    assert report.verdict == "fail"
    assert report.recommended_action == "hold"
    assert report.reason_codes == [
        "candidate_success_rate_below_minimum",
        "candidate_success_rate_drop_exceeds_limit",
    ]
    assert report.success_rate_change == -0.2
    assert report.candidate.cost_per_accepted_outcome_usd == Decimal("0.714286")


def test_comparison_investigates_cost_per_outcome_regression() -> None:
    baseline = _version(
        "v1",
        [_run(f"b-{index}", cost="1", accepted=True) for index in range(10)],
    )
    candidate = _version(
        "v2",
        [_run(f"c-{index}", cost="1.30", accepted=True) for index in range(10)],
    )

    report = compare_workflow_versions(
        baseline,
        candidate,
        policy=DecisionPolicy(max_cost_per_outcome_increase=0.1),
    )

    assert report.verdict == "fail"
    assert report.recommended_action == "investigate"
    assert report.reason_codes == ["cost_per_outcome_increase_exceeds_limit"]
    assert report.cost_per_outcome_change == 0.3


def test_comparison_approves_cheaper_candidate_with_preserved_outcomes() -> None:
    baseline = _version(
        "v1",
        [_run(f"b-{index}", cost="1", accepted=index < 9) for index in range(10)],
    )
    candidate = _version(
        "v2",
        [_run(f"c-{index}", cost="0.60", accepted=index < 9) for index in range(10)],
    )

    report = compare_workflow_versions(
        baseline,
        candidate,
        policy=DecisionPolicy(
            min_runs=10,
            min_outcome_coverage=1,
            min_success_rate=0.85,
            max_success_rate_drop=0.02,
            max_cost_per_outcome_increase=0,
        ),
    )

    assert report.verdict == "pass"
    assert report.recommended_action == "approve"
    assert report.reason_codes == ["economic_constraints_satisfied"]
    assert report.cost_per_outcome_change == -0.4


def test_comparison_abstains_from_estimated_or_missing_cost_by_default() -> None:
    baseline = _version(
        "v1",
        [_run(f"b-{index}", cost="1", accepted=True) for index in range(10)],
    )
    candidate = _version(
        "v2",
        [
            _run(
                "c-0",
                cost="0.25",
                accepted=True,
                measurement=MeasurementState.ESTIMATED,
            ),
            _run("c-1", cost=None, accepted=True, measurement=MeasurementState.UNMEASURED),
            *[_run(f"c-{index}", cost="0.25", accepted=True) for index in range(2, 10)],
        ],
    )

    report = compare_workflow_versions(baseline, candidate)

    assert report.verdict == "abstain"
    assert report.reason_codes == [
        "candidate_contains_estimated_cost",
        "candidate_contains_unmeasured_cost",
    ]
    assert report.candidate.measured_cost_usd == Decimal("2.00")
    assert report.candidate.estimated_cost_usd == Decimal("0.25")
    assert report.candidate.unmeasured_runs == 1
    assert report.candidate.cost_per_accepted_outcome_usd is None


def test_comparison_abstains_from_inferred_outcomes_by_default() -> None:
    baseline = _version(
        "v1",
        [_run(f"b-{index}", cost="1", accepted=True) for index in range(10)],
    )
    candidate = _version(
        "v2",
        [
            _run(
                "c-0",
                cost="0.5",
                accepted=True,
                outcome_measurement=MeasurementState.ESTIMATED,
            ),
            *[_run(f"c-{index}", cost="0.5", accepted=True) for index in range(1, 10)],
        ],
    )

    report = compare_workflow_versions(baseline, candidate)

    assert report.verdict == "abstain"
    assert report.reason_codes == ["candidate_contains_inferred_outcomes"]
    assert report.candidate.inferred_outcome_runs == 1

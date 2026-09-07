from __future__ import annotations

from decimal import Decimal

import pytest

from zeroth.econ.decisioning import (
    DecisionPolicy,
    EconomicDecision,
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
        policy=DecisionPolicy(min_runs=10, min_outcome_coverage=0.8, min_success_rate=0),
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
    assert report.candidate.cost_per_accepted_outcome_usd == Decimal("5") / 7


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
        policy=DecisionPolicy(max_cost_per_outcome_increase=0.1, min_success_rate=0),
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
    assert report.recommended_action == "review_candidate"
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

    report = compare_workflow_versions(baseline, candidate, policy=DecisionPolicy(min_success_rate=0))

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

    report = compare_workflow_versions(baseline, candidate, policy=DecisionPolicy(min_success_rate=0))

    assert report.verdict == "abstain"
    assert report.reason_codes == ["candidate_contains_inferred_outcomes"]
    assert report.candidate.inferred_outcome_runs == 1


@pytest.mark.parametrize("accepted", [0, 2, 4])
@pytest.mark.parametrize("cost", ["0.5", "1", "2"])
def test_version_policy_requires_an_explicit_quality_floor(accepted, cost):
    baseline = _version("v1", [_run(str(i), cost="1", accepted=True) for i in range(4)])
    candidate = _version("v2", [_run(str(i), cost=cost, accepted=i < accepted) for i in range(4)])
    report = compare_workflow_versions(baseline, candidate, policy=DecisionPolicy(min_runs=1))
    assert report.verdict == "abstain"
    assert "policy.min_success_rate" in report.reason_codes
    assert report.claim_class == "observed_comparison"
    assert report.method_version == "observed-policy/1"


def test_policy_pass_is_an_observed_result_including_when_cost_growth_is_tolerated():
    baseline = _version("v1", [_run("b", cost="1", accepted=True)])
    candidate = _version("v2", [_run("c", cost="1.09", accepted=True)])
    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_runs=1, min_success_rate=0),
    )
    assert report.verdict == "pass"
    assert report.recommended_action == "review_candidate"
    assert report.cost_per_outcome_change == 0.09
    assert report.claim_class == "observed_comparison"
    assert report.method_version == "observed-policy/1"
    assert "source_completeness_unverified" in report.limitations
    assert "no_statistical_causal_or_forecast_authorization" in report.limitations

    old_report = report.model_dump(exclude={"claim_class", "method_version", "limitations"})
    old_report["recommended_action"] = "approve"
    restored = EconomicDecision.model_validate(old_report)
    assert restored.claim_class == "legacy_unclassified"
    assert restored.method_version == "legacy_unversioned"
    assert restored.recommended_action == "approve"
    assert restored.cost_per_outcome_change == 0.09

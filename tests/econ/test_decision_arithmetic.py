"""Policy boundaries are arithmetic, independent of presentation precision."""

from decimal import Decimal
from fractions import Fraction

import pytest

from zeroth.econ.decisioning import (
    DecisionPolicy,
    RunEvidence,
    VersionEvidence,
    compare_workflow_versions,
)
from zeroth.econ.measurement import MeasurementState


def _version(version, *, cost="1", accepted=3, labeled=4, runs=4):
    return VersionEvidence(
        workflow="arithmetic",
        version=version,
        runs=[
            RunEvidence(
                run_id=f"{version}-{i}",
                cost_usd=Decimal(cost),
                cost_measurement="measured",
                accepted=i < accepted if i < labeled else None,
            )
            for i in range(runs)
        ],
    )


@pytest.mark.parametrize("scale", ["1", "0.0001", "0.00000001"])
@pytest.mark.parametrize("ratio", ["0.5", "1", "1.0999999", "1.1", "1.1000001", "2"])
@pytest.mark.parametrize("accepted", [1, 3, 4])
def test_verdict_and_cost_ratio_are_invariant_to_currency_scale(scale, ratio, accepted):
    baseline_cost = Decimal(scale)
    candidate_cost = baseline_cost * Decimal(ratio)
    report = compare_workflow_versions(
        _version("v1", cost=baseline_cost, accepted=accepted),
        _version("v2", cost=candidate_cost, accepted=accepted),
        policy=DecisionPolicy(min_runs=1, min_success_rate=0),
    )
    exact_change = Fraction(ratio) - 1
    assert report.verdict == ("fail" if exact_change > Fraction(1, 10) else "pass")
    assert report.cost_per_outcome_change == pytest.approx(float(exact_change), abs=1e-15)
    assert report.baseline.measured_cost_usd == 4 * baseline_cost
    assert report.candidate.measured_cost_usd == 4 * candidate_cost
    assert report.baseline.cost_per_accepted_outcome_usd == 4 * baseline_cost / accepted
    assert report.candidate.cost_per_accepted_outcome_usd == 4 * candidate_cost / accepted


@pytest.mark.parametrize("floor", [0.3333332, 0.3333334, 0.6666666, 0.6666667])
@pytest.mark.parametrize("accepted", [1, 2])
def test_quality_floor_uses_counts_before_rounding(floor, accepted):
    evidence = _version("v1", accepted=accepted, labeled=3, runs=3)
    report = compare_workflow_versions(
        evidence,
        evidence.model_copy(update={"version": "v2"}),
        policy=DecisionPolicy(min_runs=1, min_success_rate=floor),
    )
    assert report.verdict == (
        "fail" if Fraction(accepted, 3) < Fraction(str(floor)) else "pass"
    )


@pytest.mark.parametrize("allowed_drop", [0.3333332, 0.3333334])
def test_quality_drop_uses_counts_before_rounding(allowed_drop):
    report = compare_workflow_versions(
        _version("v1", accepted=2, labeled=3, runs=3),
        _version("v2", accepted=1, labeled=3, runs=3),
        policy=DecisionPolicy(
            min_runs=1, min_success_rate=0,
            max_success_rate_drop=allowed_drop, max_cost_per_outcome_increase=2,
        ),
    )
    assert report.verdict == ("fail" if Fraction(1, 3) > Fraction(str(allowed_drop)) else "pass")


@pytest.mark.parametrize("coverage_floor", [0.6666666, 0.6666667])
def test_coverage_reason_uses_counts_before_rounding(coverage_floor):
    report = compare_workflow_versions(
        _version("v1"),
        _version("v2", accepted=2, labeled=2, runs=3),
        policy=DecisionPolicy(
            min_runs=1, min_success_rate=0, min_outcome_coverage=coverage_floor,
        ),
    )
    assert report.verdict == "abstain"
    assert ("candidate_outcome_coverage_below_minimum" in report.reason_codes) == (
        Fraction(2, 3) < Fraction(str(coverage_floor))
    )


def test_zero_baseline_remains_undefined():
    report = compare_workflow_versions(
        _version("v1", cost="0"), _version("v2", cost="0"),
        policy=DecisionPolicy(min_runs=1, min_success_rate=0),
    )
    assert report.verdict == "abstain"
    assert report.cost_per_outcome_change is None
    assert "cost_per_outcome_comparison_unavailable" in report.reason_codes


def test_undefined_cost_comparison_precedes_quality_failure():
    report = compare_workflow_versions(
        _version("v1", cost="0"), _version("v2", cost="1", accepted=1),
        policy=DecisionPolicy(min_runs=1, min_success_rate=1),
    )
    assert report.verdict == "abstain"
    assert "cost_per_outcome_comparison_unavailable" in report.reason_codes


@pytest.mark.parametrize("scale", ["1", "0.00000001"])
@pytest.mark.parametrize("ratio", ["0.5", "1", "1.1"])
def test_different_acceptance_denominators_use_unrounded_totals(scale, ratio):
    report = compare_workflow_versions(
        _version("v1", cost=scale, accepted=3),
        _version("v2", cost=Decimal(scale) * Decimal(ratio), accepted=2),
        policy=DecisionPolicy(min_runs=1, min_success_rate=0, max_success_rate_drop=1),
    )
    exact_change = Fraction(ratio) * Fraction(3, 2) - 1
    assert report.verdict == ("fail" if exact_change > Fraction(1, 10) else "pass")
    assert report.cost_per_outcome_change == pytest.approx(float(exact_change), abs=1e-15)


def test_explicitly_allowed_estimates_contribute_to_the_ratio():
    baseline = _version("v1", cost="0.00000002")
    candidate = _version("v2", cost="0.00000001")
    for evidence in (baseline, candidate):
        evidence.runs[0].cost_measurement = MeasurementState.ESTIMATED
    report = compare_workflow_versions(
        baseline, candidate,
        policy=DecisionPolicy(min_runs=1, min_success_rate=0, allow_estimated_cost=True),
    )
    assert report.verdict == "pass"
    assert report.cost_per_outcome_change == -0.5

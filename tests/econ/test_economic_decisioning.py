from __future__ import annotations

import math
from decimal import Decimal

import numpy as np

import pytest

from zeroth.econ.decisioning import (
    COST_PER_OUTCOME_CHANGE_UNDETERMINED,
    SUCCESS_RATE_DROP_UNDETERMINED,
    SUCCESS_RATE_MINIMUM_UNDETERMINED,
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


def _runs(prefix: str, count: int, *, accepted: int, cost: str) -> list[RunEvidence]:
    return [
        _run(f"{prefix}-{index}", cost=cost, accepted=index < accepted) for index in range(count)
    ]


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
    assert report.additional_runs_required is None


def test_comparison_holds_candidate_that_breaks_success_constraint() -> None:
    # 60/100 against 100/100: the Wilson upper bound on the candidate (0.69) sits below the
    # 0.85 minimum and the Newcombe upper bound on the change (-0.30) below the -0.02
    # limit, so both breaches are established rather than merely observed.
    baseline = _version("v1", _runs("b", 100, accepted=100, cost="1"))
    candidate = _version("v2", _runs("c", 100, accepted=60, cost="0.50"))

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
    assert report.success_rate_change == -0.4
    assert report.candidate_success_rate_interval is not None
    assert report.candidate_success_rate_interval.high < 0.85
    assert report.success_rate_change_interval is not None
    assert report.success_rate_change_interval.high < -0.02
    assert report.success_rate_change_interval.method == "newcombe_hybrid_score"
    assert report.candidate.cost_per_accepted_outcome_usd == Decimal("50") / 60


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
    # Constant per-run costs and identical acceptance leave no sampling variance in the
    # ratio, so the interval is the point estimate and the breach is established.
    assert report.cost_per_outcome_change_interval is not None
    assert report.cost_per_outcome_change_interval.low == pytest.approx(0.3)
    assert report.cost_per_outcome_change_interval.high == pytest.approx(0.3)
    assert report.cost_per_outcome_change_interval.method == "delta_method_log_ratio_t"


def test_comparison_approves_cheaper_candidate_with_preserved_outcomes() -> None:
    # 300 fully accepted runs per version: the Newcombe lower bound on the change is
    # -0.013, inside the -0.02 limit, and the Wilson lower bound (0.987) clears 0.85.
    baseline = _version("v1", _runs("b", 300, accepted=300, cost="1"))
    candidate = _version("v2", _runs("c", 300, accepted=300, cost="0.60"))

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
    assert report.success_rate_change_interval is not None
    assert report.success_rate_change_interval.low >= -0.02
    assert report.candidate_success_rate_interval is not None
    assert report.candidate_success_rate_interval.low >= 0.85
    assert report.additional_runs_required is None


def test_ten_identical_runs_per_version_cannot_establish_non_inferiority() -> None:
    # The same 9/10 against 9/10 at $1 against $0.60 used to be approved outright. The
    # Newcombe interval on the change spans [-0.30, +0.30], so the drop gate is
    # undetermined; the cost gate is settled because the costs are constant.
    baseline = _version("v1", _runs("b", 10, accepted=9, cost="1"))
    candidate = _version("v2", _runs("c", 10, accepted=9, cost="0.60"))

    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_success_rate=0)
    )

    assert report.verdict == "abstain"
    assert report.recommended_action == "collect_evidence"
    assert report.reason_codes == [SUCCESS_RATE_DROP_UNDETERMINED]
    assert report.success_rate_change == 0.0
    assert report.success_rate_change_interval is not None
    assert (
        report.success_rate_change_interval.low < -0.05 < report.success_rate_change_interval.high
    )
    assert report.cost_per_outcome_change == -0.4
    assert report.cost_per_outcome_change_interval is not None
    assert report.cost_per_outcome_change_interval.high <= 0.1
    assert report.additional_runs_required is not None
    assert report.additional_runs_required > 0
    assert report.policy.confidence_level == 0.95


def test_candidate_ten_points_worse_is_neither_approved_nor_failed_at_ten_runs() -> None:
    baseline = _version("v1", _runs("b", 10, accepted=9, cost="1"))
    candidate = _version("v2", _runs("c", 10, accepted=8, cost="1"))

    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_success_rate=0)
    )

    assert report.verdict == "abstain"
    assert SUCCESS_RATE_DROP_UNDETERMINED in report.reason_codes
    assert report.success_rate_change == -0.1


def test_every_undetermined_gate_is_named_with_one_shortfall_estimate() -> None:
    rng = np.random.default_rng(4)
    baseline = _version(
        "v1",
        [
            _run(f"b-{i}", cost=str(round(float(np.exp(rng.normal(0, 0.5))), 4)), accepted=i < 8)
            for i in range(10)
        ],
    )
    candidate = _version(
        "v2",
        [
            _run(f"c-{i}", cost=str(round(float(np.exp(rng.normal(0, 0.5))), 4)), accepted=i < 8)
            for i in range(10)
        ],
    )

    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_success_rate=0.7)
    )

    assert report.verdict == "abstain"
    assert report.reason_codes == [
        SUCCESS_RATE_MINIMUM_UNDETERMINED,
        SUCCESS_RATE_DROP_UNDETERMINED,
        COST_PER_OUTCOME_CHANGE_UNDETERMINED,
    ]
    assert report.additional_runs_required is not None
    assert report.additional_runs_required > 0


def test_additional_runs_required_shrinks_as_evidence_accumulates() -> None:
    small = compare_workflow_versions(
        _version("v1", _runs("b", 10, accepted=9, cost="1")),
        _version("v2", _runs("c", 10, accepted=9, cost="0.60")),
        policy=DecisionPolicy(min_success_rate=0),
    )
    larger = compare_workflow_versions(
        _version("v1", _runs("b", 100, accepted=90, cost="1")),
        _version("v2", _runs("c", 100, accepted=90, cost="0.60")),
        policy=DecisionPolicy(min_success_rate=0),
    )

    assert small.additional_runs_required is not None
    assert larger.additional_runs_required is not None
    assert larger.additional_runs_required < small.additional_runs_required


def test_confidence_level_widens_the_intervals() -> None:
    baseline = _version("v1", _runs("b", 50, accepted=45, cost="1"))
    candidate = _version("v2", _runs("c", 50, accepted=45, cost="0.60"))

    loose = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(confidence_level=0.80, min_success_rate=0)
    )
    strict = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(confidence_level=0.99, min_success_rate=0)
    )

    assert loose.success_rate_change_interval is not None
    assert strict.success_rate_change_interval is not None
    assert loose.success_rate_change_interval.confidence_level == 0.80
    assert strict.success_rate_change_interval.confidence_level == 0.99
    assert (
        strict.success_rate_change_interval.high - strict.success_rate_change_interval.low
        > loose.success_rate_change_interval.high - loose.success_rate_change_interval.low
    )


@pytest.mark.parametrize("bad", [0.0, 1.0, 1.5, -0.1])
def test_policy_rejects_a_confidence_level_outside_the_unit_interval(bad: float) -> None:
    with pytest.raises(ValueError):
        DecisionPolicy(confidence_level=bad, min_success_rate=0)


def test_reports_stored_before_the_interval_fields_still_validate() -> None:
    report = compare_workflow_versions(
        _version("v1", _runs("b", 10, accepted=9, cost="1")),
        _version("v2", _runs("c", 10, accepted=9, cost="0.60")),
        policy=DecisionPolicy(min_success_rate=0),
    )
    stored = report.model_dump(mode="json")
    for legacy_missing in (
        "success_rate_change_interval",
        "cost_per_outcome_change_interval",
        "candidate_success_rate_interval",
        "additional_runs_required",
    ):
        stored.pop(legacy_missing)
    stored["policy"].pop("confidence_level")

    revived = EconomicDecision.model_validate(stored)

    assert revived.verdict == report.verdict
    assert revived.policy.confidence_level == 0.95
    assert revived.additional_runs_required is None


def _simulated(name: str, n: int, p_success: float, cost_mu: float, rng: np.random.Generator):
    accepted = rng.random(n) < p_success
    cost = np.exp(rng.normal(math.log(cost_mu), 0.5, size=n))
    return VersionEvidence(
        workflow="wf",
        version=name,
        runs=[
            RunEvidence(
                run_id=f"{name}-{i}",
                cost_usd=Decimal(str(round(float(c), 6))),
                cost_measurement=MeasurementState.MEASURED,
                accepted=bool(a),
            )
            for i, (a, c) in enumerate(zip(accepted, cost, strict=True))
        ],
    )


def test_decision_error_rates_stay_within_the_policy_confidence() -> None:
    # Acceptance (forecast-math validation 2026-09-06, item B2): P(fail | identical
    # versions) <= 0.05 and P(pass | candidate ten points worse) <= 0.05 at every n, and
    # P(pass | 30% cheaper, equal quality, n=1000) >= 0.90. Replications are small enough
    # to keep the module fast, so the bounds carry sampling slack; the full-size numbers
    # are in the release notes for 0.24.12.
    rng = np.random.default_rng(2026)
    replications = 150
    identical_fail = 0
    worse_pass = 0
    for _ in range(replications):
        same = compare_workflow_versions(
            _simulated("base", 30, 0.9, 1.0, rng),
            _simulated("cand", 30, 0.9, 1.0, rng),
            policy=DecisionPolicy(min_success_rate=0),
        )
        identical_fail += same.verdict == "fail"
        worse = compare_workflow_versions(
            _simulated("base", 30, 0.9, 1.0, rng),
            _simulated("cand", 30, 0.8, 1.0, rng),
            policy=DecisionPolicy(min_success_rate=0),
        )
        worse_pass += worse.verdict == "pass"
    assert identical_fail / replications <= 0.06
    assert worse_pass / replications <= 0.06

    cheaper_pass = 0
    large = 40
    for _ in range(large):
        cheaper = compare_workflow_versions(
            _simulated("base", 1000, 0.9, 1.0, rng),
            _simulated("cand", 1000, 0.9, 0.7, rng),
            policy=DecisionPolicy(min_success_rate=0),
        )
        cheaper_pass += cheaper.verdict == "pass"
    assert cheaper_pass / large >= 0.80


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

    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_success_rate=0)
    )

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

    report = compare_workflow_versions(
        baseline, candidate, policy=DecisionPolicy(min_success_rate=0)
    )

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
    assert report.method_version == "interval-policy/1"


def test_policy_pass_is_an_observed_result_including_when_cost_growth_is_tolerated():
    baseline = _version("v1", _runs("b", 100, cost="1", accepted=100))
    candidate = _version("v2", _runs("c", 100, cost="1.09", accepted=100))
    report = compare_workflow_versions(
        baseline,
        candidate,
        policy=DecisionPolicy(min_runs=1, min_success_rate=0),
    )
    assert report.verdict == "pass"
    assert report.recommended_action == "review_candidate"
    assert report.cost_per_outcome_change == 0.09
    assert report.claim_class == "observed_comparison"
    assert report.method_version == "interval-policy/1"
    assert "source_completeness_unverified" in report.limitations
    assert "no_statistical_causal_or_forecast_authorization" in report.limitations

    old_report = report.model_dump(exclude={"claim_class", "method_version", "limitations"})
    old_report["recommended_action"] = "approve"
    restored = EconomicDecision.model_validate(old_report)
    assert restored.claim_class == "legacy_unclassified"
    assert restored.method_version == "legacy_unversioned"
    assert restored.recommended_action == "approve"
    assert restored.cost_per_outcome_change == 0.09

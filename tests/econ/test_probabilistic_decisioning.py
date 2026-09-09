"""Mathematical diagnostic tests; public authorization is tested in cutoff/API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from zeroth.econ.probabilistic import (
    CohortRoutingAction,
    FORECAST_ALGORITHM_VERSION,
    ForecastReadiness,
    MigrationEvidence,
    MigrationObservation,
    MigrationRiskPolicy,
    _ExperimentalRiskQualification,
    empirical_var_cvar,
    _diagnose_model_migration,
)

QUALIFICATION_TIME = datetime(2026, 9, 3, tzinfo=UTC)


def _observations(
    prefix: str,
    *,
    count: int,
    cost: str,
    accepted_until: int,
    critical_every: int | None = None,
) -> list[MigrationObservation]:
    return [
        MigrationObservation(
            case_id=f"case-{index}",
            cohort="default",
            cost_usd=Decimal(cost),
            latency_ms=800 + (index % 5) * 10,
            accepted=index < accepted_until,
            critical_error=(critical_every is not None and index % critical_every == 0),
            source=prefix,
        )
        for index in range(count)
    ]


def _evidence(
    *,
    count: int = 100,
    incumbent_cost: str = "1.00",
    candidate_cost: str = "0.50",
    incumbent_accepted: int | None = None,
    candidate_accepted: int | None = None,
    candidate_critical_every: int | None = None,
) -> MigrationEvidence:
    return MigrationEvidence(
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        incumbent=_observations(
            "production",
            count=count,
            cost=incumbent_cost,
            accepted_until=count if incumbent_accepted is None else incumbent_accepted,
        ),
        candidate=_observations(
            "replay",
            count=count,
            cost=candidate_cost,
            accepted_until=count if candidate_accepted is None else candidate_accepted,
            critical_every=candidate_critical_every,
        ),
        period_request_counts=[90, 100, 110],
        demand_horizon="month",
        readiness=ForecastReadiness(
            calibration_state="calibrated",
            drift_state="stable",
            interval_coverage=0.95,
            relative_bias=0,
            relative_residual_shift=0,
            calibration_periods=8,
        ),
    )


def _qualified_diagnose(
    evidence: MigrationEvidence,
    *,
    policy: MigrationRiskPolicy,
    simulations: int,
    seed: int,
):
    action_ids = (
        tuple(action.action_id for action in policy.routing_actions)
        if policy.routing_actions
        else tuple(f"global-{share:g}" for share in policy.candidate_shares)
    )
    qualification = _ExperimentalRiskQualification(
        qualification_id="legacy-math-test-qualification",
        workload=evidence.workload,
        incumbent_model=evidence.incumbent_model,
        candidate_model=evidence.candidate_model,
        action_ids=action_ids,
        metric_supports=(
            ("monthly_cost_usd", (0.0, 1_000_000.0)),
            ("p95_latency_ms", (0.0, 1_000_000.0)),
            ("success_rate", (0.0, 1.0)),
            ("critical_error_rate", (0.0, 1.0)),
        ),
        loss_support=(-1_000_000.0, 1_000_000.0),
        currency="USD",
        loss_formula_version="incremental-cost-critical-penalty-v1",
        independent_unit="paired-request-and-independent-month",
        dependence_kind="independent_paired_requests_and_periods",
        artifact_sha256="b" * 64,
        issuer="test-suite",
        valid_from=QUALIFICATION_TIME,
        valid_until=QUALIFICATION_TIME,
        active=True,
    )
    return _diagnose_model_migration(
        evidence,
        policy=policy,
        simulations=simulations,
        seed=seed,
        _qualification=qualification,
        _qualification_checked_at=QUALIFICATION_TIME,
    )


def test_empirical_cvar_averages_the_declared_worst_tail() -> None:
    value_at_risk, cvar = empirical_var_cvar([1.0, 2.0, 3.0, 100.0], confidence=0.75)

    assert value_at_risk == 3.0
    assert cvar == 100.0


def test_cvar_qualification_requires_effective_tail_evidence() -> None:
    interval = getattr(
        __import__("zeroth.econ.probabilistic", fromlist=["_cvar_monte_carlo_interval"]),
        "_cvar_monte_carlo_interval",
        None,
    )
    assert interval is not None

    result = interval(
        [-100.0] * 1_000,
        confidence=0.95,
        action_count=1,
        limit=300.0,
    )

    assert result["status"] == "insufficient"
    assert result["effective_tail_samples"] == 50
    assert result["minimum_effective_tail_samples"] == 100


def test_cvar_qualification_does_not_treat_resampling_as_new_source_evidence() -> None:
    interval = getattr(
        __import__("zeroth.econ.probabilistic", fromlist=["_cvar_monte_carlo_interval"]),
        "_cvar_monte_carlo_interval",
        None,
    )
    assert interval is not None

    result = interval(
        [-100.0] * 2_000,
        confidence=0.95,
        action_count=1,
        limit=300.0,
        source_observations=400,
    )

    assert result["effective_tail_samples"] == 100
    assert result["effective_source_tail_samples"] == 20
    assert result["minimum_effective_source_tail_samples"] == 30
    assert result["additional_source_observations_required"] == 200
    assert result["status"] == "insufficient"


def test_zero_event_rate_has_finite_sample_predictive_upper_bound() -> None:
    assert FORECAST_ALGORITHM_VERSION.endswith("-predictive1")
    evidence = _evidence(count=400)
    evidence.period_request_counts = [1_000]
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=1.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=1.0,
        max_cvar_loss_usd=Decimal("0"),
    )
    report = _qualified_diagnose(evidence, policy=policy, simulations=100, seed=7)

    assert report.actions[0].critical_error_rate_p05 == 0.0
    assert report.actions[0].critical_error_rate_p95 == pytest.approx(0.017680380334649776)


def test_cvar_abstention_reports_exact_paired_case_shortfall() -> None:
    evidence = _evidence(count=400)
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=1.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=1.0,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=13)

    assert report.reason_codes == ["mc_cvar_indeterminate", "demand_history_insufficient"]
    assert report.additional_cases_required == 200


def test_zero_observed_critical_events_cannot_numerically_authorize_action() -> None:
    evidence = _evidence(count=600)
    evidence.period_request_counts = [1_000] * 12
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=1.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=0.01,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=19)

    critical = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"][
        "critical_error"
    ]
    assert critical["predictive_rate_upper"] > policy.max_critical_error_rate
    assert critical["status"] == "indeterminate"
    assert report.recommended_action == "collect_evidence"


def test_all_success_bootstrap_cannot_authorize_zero_quality_drop_limit() -> None:
    evidence = _evidence(count=600)
    evidence.period_request_counts = [1_000] * 12
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=23)

    quality = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"]["quality"]
    assert quality["predictive_quality_drop_upper"] > 0
    assert quality["status"] == "indeterminate"
    assert report.recommended_action == "collect_evidence"


def test_cvar_boundary_crossing_interval_is_indeterminate() -> None:
    interval = getattr(
        __import__("zeroth.econ.probabilistic", fromlist=["_cvar_monte_carlo_interval"]),
        "_cvar_monte_carlo_interval",
        None,
    )
    assert interval is not None
    losses = []
    for batch in range(20):
        losses.extend([0.0] * 95 + [285.0 if batch < 10 else 305.0] * 5)

    result = interval(
        losses,
        confidence=0.95,
        action_count=1,
        limit=300.0,
    )

    assert result["estimate"] == 295.0
    assert result["lower"] < 300.0 < result["upper"]
    assert result["status"] == "indeterminate"


@pytest.mark.parametrize(
    ("month", "lower", "upper", "limit", "expected"),
    [
        (38, 331.97, 339.05, 300.0, "infeasible"),
        (39, 0.115, 0.124, 0.10, "infeasible"),
        (40, 363.841, 378.429, 300.0, "infeasible"),
        (41, 359.076, 371.784, 300.0, "infeasible"),
        (45, 301.38, 314.69, 300.0, "infeasible"),
        (46, 295.0, 305.0, 300.0, "indeterminate"),
    ],
)
def test_frozen_oracle_intervals_never_qualify_crossing_or_infeasible_actions(
    month: int, lower: float, upper: float, limit: float, expected: str
) -> None:
    module = __import__("zeroth.econ.probabilistic", fromlist=["_upper_bound_status"])
    classify = getattr(module, "_upper_bound_status", None)
    assert classify is not None, f"month {month}: upper-bound interval classifier is missing"

    assert classify(lower, upper, limit=limit) == expected


def test_action_selection_abstains_when_only_cvar_evidence_is_insufficient() -> None:
    evidence = _evidence()
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=1.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=1.0,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=100, seed=11)

    assert report.actions[0].feasible is True
    assert all(
        report.evidence_lineage["numerical_qualification"]["actions"]["global-1"][metric]["status"]
        == "qualified"
        for metric in ("quality", "latency", "critical_error")
    )
    assert (
        report.evidence_lineage["numerical_qualification"]["actions"]["global-1"]["cvar"]["status"]
        == "insufficient"
    )
    assert report.recommended_action == "collect_evidence"
    assert report.reason_codes == ["mc_cvar_indeterminate", "demand_history_insufficient"]


def test_probabilistic_recommendation_is_reproducible_for_a_seed() -> None:
    evidence = _evidence()
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.02,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.05,
        max_cvar_loss_usd=Decimal("0"),
    )

    first = _qualified_diagnose(evidence, policy=policy, simulations=400, seed=17)
    second = _qualified_diagnose(evidence, policy=policy, simulations=400, seed=17)

    assert first == second
    # Point-feasible is not numerically qualified with only 400 simulations.
    assert first.actions[-1].feasible is True
    assert first.recommended_action == "collect_evidence"
    assert first.recommended_candidate_share == 0.0
    assert "mc_probability_indeterminate" in first.reason_codes
    assert first.actions[-1].expected_monthly_savings_usd > 0
    assert first.actions[-1].cvar_loss_usd < 0


def test_risk_constraints_select_a_feasible_hybrid_instead_of_full_migration() -> None:
    evidence = _evidence(count=600, incumbent_accepted=600, candidate_accepted=480)
    evidence.period_request_counts = [1_000] * 12
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[0.25, 1.0],
        max_quality_drop=0.10,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.10,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("50"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=3)

    by_share = {action.candidate_share: action for action in report.actions}
    assert by_share[1.0].feasible is False
    assert "quality_chance_constraint" in by_share[1.0].violated_constraints
    assert by_share[1.0].minimum_quality_drop_tolerance > policy.max_quality_drop
    assert by_share[0.25].feasible is True
    assert by_share[0.25].minimum_quality_drop_tolerance <= policy.max_quality_drop
    assert report.recommended_action == "hybrid_route"
    assert report.recommended_candidate_share == 0.25


def test_action_forecast_exposes_inspectable_cost_and_savings_intervals() -> None:
    evidence = _evidence()
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.02,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("0"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=400, seed=27)
    action = report.actions[0]

    assert action.monthly_cost_p05_usd <= action.expected_monthly_cost_usd
    assert action.expected_monthly_cost_usd <= action.monthly_cost_p95_usd
    assert action.monthly_savings_p05_usd <= action.expected_monthly_savings_usd
    assert action.expected_monthly_savings_usd <= action.monthly_savings_p95_usd
    assert action.minimum_cvar_loss_limit_usd == action.cvar_loss_usd
    assert action.success_rate_p05 <= action.expected_success_rate
    assert action.expected_success_rate <= action.success_rate_p95
    assert action.p95_latency_p05_ms <= action.expected_p95_latency_ms
    assert action.expected_p95_latency_ms <= action.p95_latency_p95_ms
    assert action.critical_error_rate_p05 <= action.expected_critical_error_rate
    assert action.expected_critical_error_rate <= action.critical_error_rate_p95


def test_predictive_intervals_include_future_request_outcome_variation() -> None:
    evidence = _evidence(count=100)
    evidence.period_request_counts = [1]
    evidence.candidate = [
        row.model_copy(
            update={
                "cost_usd": Decimal("0") if index < 50 else Decimal("2"),
                "latency_ms": 100 if index < 50 else 300,
                "accepted": index < 50,
            }
        )
        for index, row in enumerate(evidence.candidate)
    ]
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=1.0,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=1.0,
        max_cvar_loss_usd=Decimal("10"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=29)
    action = report.actions[0]

    assert action.monthly_cost_p05_usd == 0
    assert action.monthly_cost_p95_usd == 2
    assert action.p95_latency_p05_ms == 100
    assert action.p95_latency_p95_ms == 300
    assert action.success_rate_p05 == 0
    assert action.success_rate_p95 == 1


def test_rare_error_constraint_abstains_when_zero_failures_do_not_bound_the_rate() -> None:
    evidence = _evidence(count=100)
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.05,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.005,
        max_constraint_breach_probability=0.05,
        max_cvar_loss_usd=Decimal("100"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=200, seed=9)

    assert report.verdict == "abstain"
    assert report.recommended_action == "collect_evidence"
    assert report.actions == []
    assert report.reason_codes == ["critical_error_upper_bound_too_wide"]
    assert report.additional_cases_required == 500


def test_tightening_a_quality_constraint_cannot_make_full_migration_feasible() -> None:
    evidence = _evidence(count=100, incumbent_accepted=100, candidate_accepted=94)
    permissive = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.10,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.10,
        max_constraint_breach_probability=0.20,
        max_cvar_loss_usd=Decimal("100"),
    )
    strict = permissive.model_copy(update={"max_quality_drop": 0.01})

    permissive_report = _qualified_diagnose(evidence, policy=permissive, simulations=1_000, seed=21)
    strict_report = _qualified_diagnose(evidence, policy=strict, simulations=1_000, seed=21)

    assert permissive_report.actions[-1].feasible is True
    assert strict_report.actions[-1].feasible is False


def test_policy_rejects_an_unbounded_rollout_grid() -> None:
    with pytest.raises(ValidationError, match="at most 8 items"):
        MigrationRiskPolicy(candidate_shares=[index / 10 for index in range(1, 10)])


def test_evidence_rejects_a_pair_whose_cohort_changed_between_models() -> None:
    evidence = _evidence()
    changed = evidence.candidate[0].model_copy(update={"cohort": "enterprise"})

    with pytest.raises(ValidationError, match="paired case cohorts must match"):
        MigrationEvidence.model_validate(
            {
                **evidence.model_dump(),
                "candidate": [
                    changed.model_dump(),
                    *[row.model_dump() for row in evidence.candidate[1:]],
                ],
            }
        )


def test_optimizer_can_route_only_the_cohort_where_the_candidate_is_safe() -> None:
    count = 600
    incumbent = _observations("production", count=count, cost="1.00", accepted_until=count)
    candidate = [
        row.model_copy(
            update={
                "cohort": "enterprise" if index < count // 2 else "self-serve",
                "cost_usd": Decimal("0.40"),
                "accepted": index < count // 2,
            }
        )
        for index, row in enumerate(
            _observations("replay", count=count, cost="0.40", accepted_until=count)
        )
    ]
    incumbent = [
        row.model_copy(update={"cohort": "enterprise" if index < count // 2 else "self-serve"})
        for index, row in enumerate(incumbent)
    ]
    evidence = _evidence().model_copy(update={"incumbent": incumbent, "candidate": candidate})
    evidence.period_request_counts = [1_000] * 12
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[1.0],
        routing_actions=[
            CohortRoutingAction(
                action_id="enterprise-only",
                cohort_candidate_shares={"enterprise": 1.0, "self-serve": 0.0},
            ),
            CohortRoutingAction(
                action_id="all",
                cohort_candidate_shares={"enterprise": 1.0, "self-serve": 1.0},
            ),
        ],
        max_quality_drop=0.05,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.10,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("100"),
    )

    report = _qualified_diagnose(evidence, policy=policy, simulations=2_000, seed=31)

    by_id = {action.action_id: action for action in report.actions}
    assert by_id["enterprise-only"].feasible is True
    assert by_id["all"].feasible is False
    assert report.recommended_action == "cohort_route"
    assert report.recommended_routing == {"enterprise": 1.0, "self-serve": 0.0}

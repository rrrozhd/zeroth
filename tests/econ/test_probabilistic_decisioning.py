"""Mathematical diagnostic tests; public authorization is tested in cutoff/API tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from zeroth.econ.probabilistic import (
    CohortRoutingAction,
    ForecastReadiness,
    MigrationEvidence,
    MigrationObservation,
    MigrationRiskPolicy,
    empirical_var_cvar,
    _diagnose_model_migration,
)


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


def test_empirical_cvar_averages_the_declared_worst_tail() -> None:
    value_at_risk, cvar = empirical_var_cvar([1.0, 2.0, 3.0, 100.0], confidence=0.75)

    assert value_at_risk == 3.0
    assert cvar == 100.0


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

    first = _diagnose_model_migration(evidence, policy=policy, simulations=400, seed=17)
    second = _diagnose_model_migration(evidence, policy=policy, simulations=400, seed=17)

    assert first == second
    # Point-feasible is not numerically qualified with only 400 simulations.
    assert first.actions[-1].feasible is True
    assert first.recommended_action == "collect_evidence"
    assert first.recommended_candidate_share == 0.0
    assert "mc_probability_indeterminate" in first.reason_codes
    assert first.actions[-1].expected_monthly_savings_usd > 0
    assert first.actions[-1].cvar_loss_usd < 0


def test_risk_constraints_select_a_feasible_hybrid_instead_of_full_migration() -> None:
    evidence = _evidence(count=100, incumbent_accepted=100, candidate_accepted=80)
    policy = MigrationRiskPolicy(
        min_paired_cases=30,
        candidate_shares=[0.25, 1.0],
        max_quality_drop=0.10,
        max_p95_latency_ms=1_000,
        max_critical_error_rate=0.10,
        max_constraint_breach_probability=0.10,
        max_cvar_loss_usd=Decimal("50"),
    )

    report = _diagnose_model_migration(evidence, policy=policy, simulations=1_000, seed=3)

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

    report = _diagnose_model_migration(evidence, policy=policy, simulations=400, seed=27)
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

    report = _diagnose_model_migration(evidence, policy=policy, simulations=200, seed=9)

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

    permissive_report = _diagnose_model_migration(
        evidence, policy=permissive, simulations=1_000, seed=21
    )
    strict_report = _diagnose_model_migration(evidence, policy=strict, simulations=1_000, seed=21)

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
    count = 100
    incumbent = _observations("production", count=count, cost="1.00", accepted_until=count)
    candidate = [
        row.model_copy(
            update={
                "cohort": "enterprise" if index < 50 else "self-serve",
                "cost_usd": Decimal("0.40"),
                "accepted": index < 50,
            }
        )
        for index, row in enumerate(
            _observations("replay", count=count, cost="0.40", accepted_until=count)
        )
    ]
    incumbent = [
        row.model_copy(update={"cohort": "enterprise" if index < 50 else "self-serve"})
        for index, row in enumerate(incumbent)
    ]
    evidence = _evidence().model_copy(update={"incumbent": incumbent, "candidate": candidate})
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

    report = _diagnose_model_migration(evidence, policy=policy, simulations=1_000, seed=31)

    by_id = {action.action_id: action for action in report.actions}
    assert by_id["enterprise-only"].feasible is True
    assert by_id["all"].feasible is False
    assert report.recommended_action == "cohort_route"
    assert report.recommended_routing == {"enterprise": 1.0, "self-serve": 0.0}

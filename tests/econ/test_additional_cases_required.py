"""Abstentions on the quality, critical-error and CVaR certificates say how much evidence
would let them qualify, or that no finite paired-case count can (report §2 A2)."""

from __future__ import annotations

from math import ceil, log, sqrt

import pytest

from tests.econ._forecast_fixtures import diagnose, evidence, policy
from zeroth.econ import probabilistic as subject


def test_case_search_matches_brute_force_on_a_monotone_bound():
    def bound(factor: float) -> float:
        return 0.5 / sqrt(factor * 100)

    required = subject._paired_cases_required(
        bound, paired_cases=100, limit=0.01, bound_at_infinite_scale=0.0
    )
    brute = next(n for n in range(100, 10_000) if bound(n / 100) <= 0.01)

    assert required == brute == 2_500
    assert (
        subject._paired_cases_required(
            bound, paired_cases=100, limit=0.1, bound_at_infinite_scale=0.0
        )
        == 100
    )
    assert (
        subject._paired_cases_required(
            bound, paired_cases=100, limit=0.01, bound_at_infinite_scale=0.02
        )
        is None
    )
    assert (
        subject._paired_cases_required(
            bound, paired_cases=100, limit=1e-9, bound_at_infinite_scale=0.0
        )
        is None
    )


def test_quality_certificate_reports_a_reachable_and_minimal_case_requirement():
    world = evidence(count=600, candidate_accepted=594)
    report = diagnose(world, policy(max_quality_drop=0.02, max_constraint_breach_probability=1.0))
    quality = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"]["quality"]

    assert report.verdict == "abstain"
    assert report.reason_codes == ["mc_probability_indeterminate"]
    assert quality["envelope_status"] == "indeterminate"
    assert quality["reachable_by_additional_cases"] is True
    assert quality["additional_cases_required"] == report.additional_cases_required > 0

    incumbent, candidate = subject._paired_observations(world)
    baseline = subject._routed_rate_components(
        incumbent, candidate, share=0.0, cohort_shares={}, attribute="accepted"
    )
    routed = subject._routed_rate_components(
        incumbent, candidate, share=1.0, cohort_shares={}, attribute="accepted"
    )
    required = 600 + report.additional_cases_required
    bound = subject._quality_drop_bound_at_scale
    assert (
        bound(required / 600, baseline_components=baseline, action_components=routed, future_trials=1_000)
        <= 0.02
    )
    assert (
        bound(
            (required - 1) / 600,
            baseline_components=baseline,
            action_components=routed,
            future_trials=1_000,
        )
        > 0.02
    )


def test_unreachable_quality_limit_is_disclosed_instead_of_asking_for_more_cases():
    report = diagnose(
        evidence(count=600, candidate_accepted=570),
        policy(max_quality_drop=0.02, max_constraint_breach_probability=1.0),
    )
    quality = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"]["quality"]

    assert report.verdict == "abstain"
    assert report.reason_codes == ["mc_probability_indeterminate", "evidence_requirement_unreachable"]
    assert report.additional_cases_required == 0
    assert quality["reachable_by_additional_cases"] is False
    assert quality["additional_cases_required"] is None


def test_zero_event_critical_certificate_reports_case_requirement():
    world = evidence(count=600)
    report = diagnose(world, policy(max_critical_error_rate=0.01, max_constraint_breach_probability=0.10))
    critical = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"][
        "critical_error"
    ]

    assert critical["status"] == "indeterminate"
    assert critical["envelope_status"] == "indeterminate"
    assert critical["reachable_by_additional_cases"] is True
    assert report.additional_cases_required == critical["additional_cases_required"] > 0

    incumbent, candidate = subject._paired_observations(world)
    routed = subject._routed_rate_components(
        incumbent, candidate, share=1.0, cohort_shares={}, attribute="critical_error"
    )
    required = 600 + report.additional_cases_required
    assert (
        subject._critical_rate_bound_at_scale(required / 600, components=routed, future_trials=1_000)
        <= 0.01
    )
    assert (
        subject._critical_rate_bound_at_scale(
            (required - 1) / 600, components=routed, future_trials=1_000
        )
        > 0.01
    )


def test_requirement_is_the_fewest_cases_over_saving_actions():
    report = diagnose(
        evidence(count=600, candidate_accepted=594),
        policy(
            candidate_shares=[0.5, 1.0],
            max_quality_drop=0.02,
            max_constraint_breach_probability=1.0,
        ),
    )
    actions = report.evidence_lineage["numerical_qualification"]["actions"]
    half = actions["global-0.5"]["quality"]["additional_cases_required"]
    full = actions["global-1"]["quality"]["additional_cases_required"]

    assert report.verdict == "abstain"
    assert 0 <= half < full
    assert report.additional_cases_required == half


def test_cvar_certificate_reports_source_and_simulation_requirements():
    # 1,000 simulations: the Hoeffding radius alone (0.058) would leave a 5% chance
    # limit indeterminate, so the chance limit is relaxed to isolate the CVaR certificate.
    report = diagnose(
        evidence(count=400, periods=12),
        policy(max_constraint_breach_probability=1.0),
        simulations=1_000,
    )
    cvar = report.evidence_lineage["numerical_qualification"]["actions"]["global-1"]["cvar"]

    assert report.reason_codes == ["mc_cvar_indeterminate"]
    assert cvar["status"] == "insufficient"
    assert cvar["additional_source_observations_required"] == 200
    assert cvar["additional_simulations_required"] == 981
    assert report.additional_cases_required == 200
    assert subject._simulations_required_for_cvar_tail(0.95, simulations=1_981) == 0
    assert subject._simulations_required_for_cvar_tail(0.99, simulations=100) == 9_801


def test_hoeffding_simulation_requirement_assumes_the_estimate_persists():
    # radius(N) = sqrt(log(8A/0.01) / 2N) <= |p - limit|  <=>  N >= log(8A/0.01) / (2 gap^2)
    expected = ceil(log(8 / 0.01) / (2 * 0.02**2)) - 1_000
    assert (
        subject._simulations_required_for_radius(
            0.03, limit=0.05, action_count=1, simulations=1_000
        )
        == expected
        == 7_356
    )
    assert (
        subject._simulations_required_for_radius(
            0.05, limit=0.05, action_count=1, simulations=1_000
        )
        is None
    )
    assert (
        subject._simulations_required_for_radius(
            0.0, limit=0.05, action_count=1, simulations=10_000
        )
        == 0
    )

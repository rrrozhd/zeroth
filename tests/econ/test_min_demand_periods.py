"""Demand uncertainty is the empirical law of the history months, so a short history
under-disperses the cost band; the policy floor ``min_demand_periods`` abstains with
``demand_history_insufficient`` and the periods still required (report §2 A5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.econ._forecast_fixtures import diagnose, evidence, policy
from zeroth.econ import probabilistic as subject

DEFAULT = subject.DEFAULT_MIN_DEMAND_PERIODS


def test_default_is_the_measured_floor_and_must_be_positive():
    assert subject.MigrationRiskPolicy().min_demand_periods == DEFAULT
    assert DEFAULT >= 6
    with pytest.raises(ValidationError):
        subject.MigrationRiskPolicy(min_demand_periods=0)


def test_short_history_abstains_with_the_periods_still_required_but_keeps_the_forecasts():
    report = diagnose(evidence(count=600, periods=3), policy())

    assert report.verdict == "abstain"
    assert report.recommended_action == "collect_evidence"
    assert report.reason_codes == ["demand_history_insufficient"]
    assert report.additional_demand_periods_required == DEFAULT - 3
    assert report.additional_cases_required == 0
    assert len(report.actions) == 1 and report.actions[0].feasible
    assert report.evidence_lineage["demand_history"] == {
        "periods": 3,
        "minimum_periods": DEFAULT,
        "additional_periods_required": DEFAULT - 3,
        "law": "empirical distribution of the observed period request counts",
    }


def test_enough_history_or_a_lower_floor_recommends():
    assert diagnose(evidence(count=600, periods=DEFAULT), policy()).verdict == "recommend"
    lowered = diagnose(evidence(count=600, periods=3), policy(min_demand_periods=3))

    assert lowered.verdict == "recommend"
    assert lowered.recommended_action == "ship_candidate"
    assert lowered.additional_demand_periods_required == 0


def test_demand_shortfall_is_appended_to_certificate_abstentions():
    report = diagnose(
        evidence(count=400, periods=3),
        policy(max_constraint_breach_probability=1.0),
        simulations=1_000,
    )

    assert report.reason_codes == ["mc_cvar_indeterminate", "demand_history_insufficient"]
    assert report.additional_cases_required == 200
    assert report.additional_demand_periods_required == DEFAULT - 3


def test_a_hold_is_not_turned_into_an_abstention_by_a_short_history():
    report = diagnose(evidence(count=600, periods=1, candidate_cost="1.00"), policy())

    assert report.verdict == "hold"
    assert report.recommended_action == "keep_incumbent"
    assert report.reason_codes == ["no_risk_feasible_saving_action"]


def test_early_abstentions_also_report_the_demand_shortfall():
    report = diagnose(evidence(count=10, periods=1), policy(min_paired_cases=30))

    assert report.reason_codes == ["paired_cases_below_minimum"]
    assert report.additional_cases_required == 20
    assert report.additional_demand_periods_required == DEFAULT - 1

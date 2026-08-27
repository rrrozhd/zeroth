from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from release.live_evaluation.criteria import original_acceptance_criteria


_PROFILE = (
    Path(__file__).parents[2]
    / "release"
    / "live_evaluation"
    / "accelerated-acceptance-v2.json"
)


def _profile() -> dict[str, object]:
    value = json.loads(_PROFILE.read_text())
    assert isinstance(value, dict)
    return value


def test_v2_is_unarmed_and_preserves_the_full_campaign_claim_boundary() -> None:
    profile = _profile()

    assert profile["status"] == "proposed_unarmed"
    assert profile["authorization_phrase"] == "AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V2"
    assert profile["claim_label"] == "demo_ready_not_full_campaign_accepted"
    assert profile["supersedes_for_future_execution_only"] == (
        "evaluation-studio-v1-accelerated-demo"
    )
    assert profile["invariants"] == {
        "mutates_original_acceptance_catalog": False,
        "permits_full_campaign_acceptance_claim": False,
        "automatic_retry_after_ambiguous_outcome": False,
        "provider_calls_before_explicit_authorization": False,
        "one_case_rightsizing_proves_model_quality": False,
        "mutates_accelerated_v1_evidence": False,
    }


def test_v2_matches_the_truthful_one_case_call_formula_and_total_caps() -> None:
    profile = _profile()
    budgets = profile["budgets"]
    gates = profile["gates"]

    assert Decimal(budgets["tenant_ceiling_usd"]) == Decimal("10.00")
    assert Decimal(budgets["per_run_ceiling_usd"]) == Decimal("0.25")
    assert Decimal(budgets["accelerated_incremental_ceiling_usd"]) == Decimal("1.00")
    assert budgets["maximum_new_live_runs"] == 3
    assert budgets["maximum_new_provider_calls"] == 15
    assert sum(gate["maximum_new_live_runs"] for gate in gates) == 3
    assert sum(gate["maximum_provider_calls"] for gate in gates) == 15

    rightsizing = next(
        gate for gate in gates if gate["gate_id"] == "accelerated-v2.rightsizing.one-case-plumbing"
    )
    assert rightsizing["maximum_candidates"] == 1
    assert rightsizing["maximum_cases"] == 1
    assert rightsizing["maximum_provider_calls"] == 4
    assert rightsizing["minimum_cases_for_confirmation"] > rightsizing["maximum_cases"]


def test_v2_uses_only_known_original_criteria_and_preserves_exclusions() -> None:
    profile = _profile()
    known = {criterion.criterion_id for criterion in original_acceptance_criteria()}
    eligible = {
        criterion_id
        for gate in profile["gates"]
        for criterion_id in gate["original_criteria_eligible"]
    }
    deferred = set(profile["deferred_original_criteria"])

    assert eligible <= known
    assert deferred <= known
    assert eligible.isdisjoint(deferred)
    assert len(profile["stop_conditions"]) == len(set(profile["stop_conditions"]))
    assert len(profile["gates"]) == len({gate["gate_id"] for gate in profile["gates"]})
    assert any("historical synthetic" in item for item in profile["preconditions"])
    assert any("historical synthetic" in item for item in profile["stop_conditions"])

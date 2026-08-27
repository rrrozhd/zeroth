from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from release.live_evaluation.criteria import original_acceptance_criteria


_PROFILE = (
    Path(__file__).parents[2]
    / "release"
    / "live_evaluation"
    / "accelerated-acceptance-v1.json"
)
_READINESS_CAMPAIGN = _PROFILE.with_name("FULL_READINESS_CAMPAIGN.md")


def _profile() -> dict[str, object]:
    value = json.loads(_PROFILE.read_text())
    assert isinstance(value, dict)
    return value


def test_accelerated_profile_is_unarmed_and_cannot_claim_full_acceptance() -> None:
    profile = _profile()
    invariants = profile["invariants"]

    assert profile["status"] == "proposed_unarmed"
    assert profile["claim_label"] == "demo_ready_not_full_campaign_accepted"
    assert profile["authorization_phrase"] == "AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE"
    assert invariants == {
        "mutates_original_acceptance_catalog": False,
        "permits_full_campaign_acceptance_claim": False,
        "automatic_retry_after_ambiguous_outcome": False,
        "provider_calls_before_explicit_authorization": False,
    }


def test_accelerated_profile_is_stricter_than_campaign_budget_and_bounded() -> None:
    profile = _profile()
    budgets = profile["budgets"]
    gates = profile["gates"]

    assert Decimal(budgets["tenant_ceiling_usd"]) == Decimal("10.00")
    assert Decimal(budgets["per_run_ceiling_usd"]) == Decimal("0.25")
    assert Decimal(budgets["accelerated_incremental_ceiling_usd"]) == Decimal("1.00")
    assert budgets["maximum_new_live_runs"] == 3
    assert budgets["maximum_new_provider_calls"] == 17
    assert sum(gate["maximum_new_live_runs"] for gate in gates) == 3
    assert sum(gate["maximum_provider_calls"] for gate in gates) == 17
    rightsizing = next(
        gate for gate in gates if gate["gate_id"] == "accelerated.rightsizing.measured-ui"
    )
    assert rightsizing["maximum_candidates"] == 1
    assert rightsizing["maximum_cases"] == 3


def test_accelerated_profile_names_only_known_original_criteria() -> None:
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


def test_full_readiness_document_preserves_the_accelerated_claim_boundary() -> None:
    document = _READINESS_CAMPAIGN.read_text()

    assert "`demo_ready_not_full_campaign_accepted`" in document
    assert "123 pass, 0 fail, 15 blocked, 3 not run" in document
    assert "does not constitute\nfull product-surface acceptance" in document
    assert "maximum incremental exposure is three new\nlive runs, 17 provider calls" in document
    assert "The accelerated campaign proves the end-to-end demo" in document

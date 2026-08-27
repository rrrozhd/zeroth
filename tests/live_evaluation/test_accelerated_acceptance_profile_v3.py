from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "release" / "live_evaluation" / "accelerated-acceptance-v3.json"


def _profile() -> dict[str, object]:
    value = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v3_is_unarmed_and_cannot_rewrite_prior_evidence() -> None:
    profile = _profile()

    assert profile["profile_id"] == "evaluation-studio-v1-accelerated-demo-v3"
    assert profile["status"] == "proposed_unarmed"
    assert profile["authorization_phrase"] == "AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V3"
    assert profile["supersedes_for_future_execution_only"] == (
        "evaluation-studio-v1-accelerated-demo-v2"
    )
    assert profile["claim_label"] == "demo_ready_not_full_campaign_accepted"
    assert profile["invariants"] == {
        "mutates_original_acceptance_catalog": False,
        "permits_full_campaign_acceptance_claim": False,
        "provider_calls_before_explicit_authorization": False,
        "mutates_accelerated_v1_evidence": False,
        "mutates_accelerated_v2_evidence": False,
        "one_case_rightsizing_proves_model_quality": False,
    }


def test_v3_contains_only_the_two_unexecuted_paid_gates() -> None:
    profile = _profile()
    budgets = profile["budgets"]
    assert isinstance(budgets, dict)
    assert budgets["maximum_new_live_runs"] == 2
    assert budgets["maximum_new_provider_calls"] == 12
    assert Decimal(str(budgets["accelerated_incremental_ceiling_usd"])) == Decimal("1.00")

    gates = profile["gates"]
    assert isinstance(gates, list)
    paid = [gate for gate in gates if gate["maximum_provider_calls"] > 0]
    assert [gate["gate_id"] for gate in paid] == [
        "accelerated-v3.workflow2.third-repetition",
        "accelerated-v3.rightsizing.one-case-plumbing",
    ]
    assert [gate["maximum_provider_calls"] for gate in paid] == [8, 4]
    assert sum(gate["maximum_new_live_runs"] for gate in paid) == 2

    rightsizing = paid[1]
    assert rightsizing["maximum_candidates"] == 1
    assert rightsizing["maximum_cases"] == 1
    assert rightsizing["minimum_cases_for_confirmation"] == 5


def test_v3_requires_the_sealed_w1_remediation_as_a_precondition() -> None:
    profile = _profile()
    preconditions = profile["preconditions"]

    assert any("accelerated-v2-cost-rollup-remediation-20260826-1" in row for row in preconditions)
    assert any("batch-provider-live-closeout-20260826-1" in row for row in preconditions)
    assert all("workflow1.live-grounded-run" not in gate["gate_id"] for gate in profile["gates"])

from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation.workflow3_current_negative_checkpoint import (
    ACCEPTED_CRITERIA,
    _validate_negative_proof,
)


def _proof(*, scenario: str) -> dict[str, object]:
    digests = [f"{'a' * 63}{index}" for index in range(1, 4)]
    return {
        "scenario": scenario,
        "run_id": f"{scenario}-run",
        "runtime": {
            "status": "failed",
            "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
            "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
            "tenant_id": "evaluation-studio-v1",
            "campaign_id": "evaluation-studio-v1",
            "persistent_status": "FAILED",
            "persistent_identity_matches": True,
            "failure_reason": "approval_rejected",
        },
        "approval": {
            "count": 1,
            "approval_id": f"{scenario}-approval",
            "source_approval_id": f"{scenario}-approval",
            "status": "resolved",
            "decision": "reject",
            "actor_subject": (
                "sla_enforcer" if scenario == "sla_expiry" else "evaluation-a-platform-admin"
            ),
            "created_at": "2026-08-24T23:07:51+00:00",
            "sla_deadline": "2026-08-24T23:07:56+00:00",
            "resolved_at": (
                "2026-08-24T23:08:00+00:00"
                if scenario == "sla_expiry"
                else "2026-08-24T23:07:54+00:00"
            ),
            "refresh_identity_assertion_passed": scenario == "refresh_reject",
            "refresh_pending_state_assertion_passed": scenario == "refresh_reject",
        },
        "verification": {
            "verified": True,
            "signature_verified": True,
            "record_count": 3,
            "unsigned_record_count": 0,
        },
        "audits": [
            {
                "audit_id": f"audit-{index}",
                "node_id": node,
                "chain_sequence": index,
                "record_digest": digests[index - 1],
                "previous_record_digest": None if index == 1 else digests[index - 2],
                "record_signature_present": True,
                "manifest_ref_sha256": None,
                "cost_usd": 0.0,
                "cost_event_id": None,
            }
            for index, node in enumerate(("request", "approval", "approval"), start=1)
        ],
        "side_effects": {
            "operation_count": 0,
            "action_manifest_audit_count": 0,
            "markers_created_since_run": 0,
        },
        "economics": {"execution_event_count": 0, "reservation_count": 0},
        "runtime_evidence_matches_database": True,
    }


def test_negative_proof_accepts_refresh_and_sla_only_with_zero_side_effects() -> None:
    _validate_negative_proof(_proof(scenario="refresh_reject"))
    _validate_negative_proof(_proof(scenario="sla_expiry"))

    action_leaked = deepcopy(_proof(scenario="refresh_reject"))
    action_leaked["side_effects"]["markers_created_since_run"] = 1  # type: ignore[index]
    with pytest.raises(RuntimeError, match="zero-side-effect"):
        _validate_negative_proof(action_leaked)

    action_audit = deepcopy(_proof(scenario="sla_expiry"))
    action_audit["audits"][2]["manifest_ref_sha256"] = "f" * 64  # type: ignore[index]
    with pytest.raises(RuntimeError, match="action-manifest audit"):
        _validate_negative_proof(action_audit)


def test_sla_proof_requires_sla_enforcer_and_resolution_after_deadline() -> None:
    early = deepcopy(_proof(scenario="sla_expiry"))
    early["approval"]["resolved_at"] = "2026-08-24T23:07:55+00:00"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="after its deadline"):
        _validate_negative_proof(early)

    wrong_actor = deepcopy(_proof(scenario="sla_expiry"))
    wrong_actor["approval"]["actor_subject"] = "operator"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="sla_enforcer"):
        _validate_negative_proof(wrong_actor)


def test_refresh_proof_requires_same_approval_identity_and_pending_state() -> None:
    changed = deepcopy(_proof(scenario="refresh_reject"))
    changed["approval"]["source_approval_id"] = "replacement"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="refresh did not preserve"):
        _validate_negative_proof(changed)

    state_lost = deepcopy(_proof(scenario="refresh_reject"))
    state_lost["approval"]["refresh_pending_state_assertion_passed"] = False  # type: ignore[index]
    with pytest.raises(RuntimeError, match="refresh did not preserve"):
        _validate_negative_proof(state_lost)


def test_negative_checkpoint_acceptance_allowlist_is_exact() -> None:
    assert ACCEPTED_CRITERIA == (
        "workflow3.negative-refresh-before-approval",
        "workflow3.negative-sla-expiry",
    )

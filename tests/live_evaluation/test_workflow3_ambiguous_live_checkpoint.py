from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation import workflow3_ambiguous_live_checkpoint as checkpoint
from release.live_evaluation.workflow3_ambiguous_live_checkpoint import validate_proof


def _proof() -> dict[str, object]:
    return {
        "health": {
            "status": "ok",
            "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
            "deployment_version": 3,
            "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
            "campaign_id": "evaluation-studio-v1",
        },
        "run_id": "run-live-1",
        "approval": {"decision": "approve", "status": "resolved"},
        "marker": {"baseline": 4, "after_commit": 5, "final": 5},
        "operation": {
            "operation_key": "operation-live-1",
            "after_timeout": {"state": "AMBIGUOUS", "reconciliation_attempts": 0},
            "after_lookup": {"state": "AMBIGUOUS", "reconciliation_attempts": 1},
            "after_refusal": {"state": "AMBIGUOUS", "reconciliation_attempts": 1},
            "after_resolution": {"state": "COMPLETED", "reconciliation_attempts": 1},
        },
        "fault": {
            "target": "action_outcome_lookup",
            "mode": "unavailable",
            "consumed": True,
        },
        "attempts": {
            "action_first_execution_count": 1,
            "authoritative_lookup_attempt_count": 1,
            "automatic_reexecution_count": 0,
        },
        "dispatch_refusal": {
            "first_public_status": "waiting_interrupt",
            "second_http_status": 409,
            "final_public_status": "waiting_interrupt",
        },
        "resolution": {
            "http_status": 200,
            "state": "COMPLETED",
            "signed_audit_count": 1,
        },
        "chain": {"verified": True, "signature_verified": True, "unsigned_record_count": 0},
    }


def test_validate_proof_accepts_exact_ambiguous_then_authorized_resolution() -> None:
    validate_proof(_proof())


def test_replay_accepts_the_public_queued_status(monkeypatch) -> None:
    class Api:
        def request(self, method: str, path: str):
            assert method == "POST"
            assert path == "/v1/admin/runs/run-live-1/replay"
            return 200, {"run_id": "run-live-1", "status": "queued"}

    expected = {"run_id": "run-live-1", "status": "waiting_interrupt"}
    monkeypatch.setattr(checkpoint, "_wait_run", lambda *_args, **_kwargs: expected)

    assert checkpoint._replay(Api(), "run-live-1") == expected


def test_repeated_replay_is_refused_while_reconciliation_is_paused(monkeypatch) -> None:
    class Api:
        def request(self, method: str, path: str, *, expected: set[int]):
            assert method == "POST"
            assert path == "/v1/admin/runs/run-live-1/replay"
            assert expected == {409}
            return 409, {"detail": "only failed runs can be replayed"}

    paused = {"run_id": "run-live-1", "status": "waiting_interrupt"}
    monkeypatch.setattr(checkpoint, "_wait_run", lambda *_args, **_kwargs: paused)

    assert checkpoint._assert_replay_refused(Api(), "run-live-1") == {
        "http_status": 409,
        "run": paused,
    }


def test_signed_initial_action_audit_reconstructs_pre_lookup_ambiguity() -> None:
    audits = [
        {
            "audit_id": "run-live-1:audit:3",
            "node_id": "synthetic-action",
            "status": "failed",
            "record_signature": "signed",
            "execution_metadata": {
                "manifest_ref_sha256": checkpoint.EVALUATION_ACTION_MANIFEST_SHA256,
                "operation_key": "operation-live-1",
                "operation_state": "ambiguous",
            },
        }
    ]

    assert checkpoint._initial_ambiguous_snapshot(audits, "operation-live-1") == {
        "state": "AMBIGUOUS",
        "reconciliation_attempts": 0,
        "signed_audit_id": "run-live-1:audit:3",
    }


def test_action_attempt_count_accepts_signed_manifest_audit_without_legacy_flag() -> None:
    audits = [
        {
            "audit_id": "run-live-1:audit:3",
            "record_signature": "signed",
            "execution_metadata": {
                "manifest_ref_sha256": checkpoint.EVALUATION_ACTION_MANIFEST_SHA256,
                "operation_state": "ambiguous",
            },
        },
        {
            "audit_id": "run-live-1:audit:4",
            "record_signature": "signed",
            "execution_metadata": {"operation_reconciliation_exhausted": True},
        },
    ]

    assert checkpoint._count_attempts(audits, 1) == {
        "action_first_execution_count": 1,
        "authoritative_lookup_attempt_count": 1,
        "automatic_reexecution_count": 0,
        "action_audit_count": 1,
    }


def test_signed_approval_api_audit_reconstructs_explicit_approval() -> None:
    audits = [
        {
            "audit_id": "approval-api:approval-live-1:approve",
            "node_id": "approval",
            "status": "approval_api",
            "record_signature": "signed",
            "execution_metadata": {},
        }
    ]

    assert checkpoint._approved_snapshot(audits) == {
        "approval_id": "approval-live-1",
        "status": "resolved",
        "decision": "approve",
        "signed_audit_id": "approval-api:approval-live-1:approve",
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("health", "graph_version_ref"), "evaluation-studio-v1-governed-remediation@3"),
        (("marker", "final"), 6),
        (("operation", "after_lookup", "state"), "COMPLETED"),
        (("operation", "after_refusal", "reconciliation_attempts"), 2),
        (("attempts", "action_first_execution_count"), 2),
        (("attempts", "authoritative_lookup_attempt_count"), 0),
        (("attempts", "automatic_reexecution_count"), 1),
        (("resolution", "signed_audit_count"), 0),
        (("chain", "verified"), False),
    ],
)
def test_validate_proof_fails_closed_on_any_broken_invariant(
    path: tuple[str, ...], value: object
) -> None:
    proof = deepcopy(_proof())
    cursor = proof
    for field in path[:-1]:
        child = cursor[field]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = value

    with pytest.raises(RuntimeError):
        validate_proof(proof)

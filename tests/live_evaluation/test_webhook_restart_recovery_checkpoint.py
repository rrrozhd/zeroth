from __future__ import annotations

import importlib
from copy import deepcopy

import pytest


def _module():
    return importlib.import_module(
        "release.live_evaluation.webhook_restart_recovery_checkpoint"
    )


def _proof() -> dict[str, object]:
    health = {
        "status": "ok",
        "campaign_id": "evaluation-studio-v1-retention-disposable",
        "deployment_ref": "demo-artifact-output-v1",
        "graph_version_ref": "evaluation-studio-v1-artifact-output@2",
    }
    subscription = {
        "subscription_id": "sub-flaky",
        "active": True,
        "target_url_mode": "flaky",
        "event_types": ["run.completed"],
    }
    dead_letter = {
        "dead_letter_id": "dead-1",
        "subscription_id": "sub-flaky",
        "event_id": "event-flaky",
        "run_id": "run-flaky",
        "attempt_count": 5,
    }
    replay = {
        "delivery_id": "delivery-replay",
        "subscription_id": "sub-flaky",
        "event_id": "event-flaky",
        "run_id": "run-flaky",
        "status": "delivered",
        "attempt_count": 1,
    }
    lease_before = {
        "delivery_id": "delivery-lease",
        "subscription_id": "sub-lease",
        "event_id": "event-lease",
        "run_id": "run-lease",
        "status": "delivering",
        "attempt_count": 1,
    }
    lease_after = {**lease_before, "status": "delivered", "attempt_count": 2}
    return {
        "schema_version": 1,
        "health_before": health,
        "health_after_restart_1": health,
        "health_after_restart_2": health,
        "restart_count": 2,
        "container_started_at": ["start-0", "start-1", "start-2"],
        "subscription_before_restart": subscription,
        "subscription_after_restart": subscription,
        "dead_letter_before_restart": dead_letter,
        "dead_letter_after_restart_1": dead_letter,
        "dead_letter_after_restart_2": dead_letter,
        "replay_after_restart_1": replay,
        "replay_after_restart_2": replay,
        "leased_before_restart": lease_before,
        "lease_recovered_after_restart": lease_after,
        "lease_sink": {
            "durable_marker_count": 1,
            "receipt_count": 1,
            "receipt_event_id": "event-lease",
            "receipt_attempt_count": 2,
            "signature_verified": True,
        },
        "provider_calls_performed": 0,
        "external_network_calls": 0,
        "subscription_cleanup": {"sub-flaky": "inactive", "sub-lease": "inactive"},
    }


def test_validate_proof_accepts_exact_restart_recovery_correlation() -> None:
    module = _module()

    module.validate_proof(_proof())


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("restart_count",), 1, "exactly two"),
        (("dead_letter_after_restart_2", "event_id"), "other", "dead-letter"),
        (("lease_recovered_after_restart", "attempt_count"), 1, "leased delivery"),
        (("lease_sink", "receipt_count"), 2, "one receipt"),
        (("provider_calls_performed",), 1, "provider-free"),
        (("container_started_at",), ["same", "same", "same"], "container restart"),
    ],
)
def test_validate_proof_rejects_incomplete_or_relabelled_restart_evidence(
    path: tuple[str, ...], value: object, message: str
) -> None:
    module = _module()
    proof = deepcopy(_proof())
    target = proof
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index,assignment]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(RuntimeError, match=message):
        module.validate_proof(proof)

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from release.live_evaluation.action_sink import EvaluationActionSink
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.fault_control import EvaluationFaultState
from release.live_evaluation.scenario_controller import (
    ScenarioControllerRuntimeGateway,
    create_scenario_controller_app,
)


class _Gateway(ScenarioControllerRuntimeGateway):
    def __init__(self) -> None:
        self.checkpoints: list[tuple[str, str]] = []
        self.cleaned: list[str] = []

    def checkpoint(self, fixture, checkpoint: str):
        self.checkpoints.append((fixture.fixture_id, checkpoint))
        return {"state": "applied", "run_id": "run-123"}

    def restart_status(self, fixture):
        return {"state": "ready", "run_id": "run-123"}

    def verify(self, fixture):
        return {
            "run_status": fixture.expected["run_status"],
            "marker_count": 0,
            "reexecution_count": 0,
            "partial_collection_count": 7,
            "operation_status": "failed",
            "run_id": "run-123",
            "audit_event_id": "audit-123",
            "cost_event_id": "cost-123",
        }

    def cleanup(self, fixture):
        self.cleaned.append(fixture.fixture_id)
        return {"state": "cleaned"}


def _client(tmp_path: Path, *, gateway=None) -> tuple[TestClient, EvidenceStore]:
    artifacts = tmp_path / "artifacts"
    evidence = EvidenceStore(artifacts / "evidence")
    app = create_scenario_controller_app(
        campaign_id="evaluation-studio-v1",
        artifact_root=artifacts,
        evidence_store=evidence,
        fault_state=EvaluationFaultState(artifacts / "faults.sqlite3"),
        action_sink=EvaluationActionSink(artifacts / "action-sink"),
        controller_key="controller-key",
        runtime_gateway=gateway,
    )
    return TestClient(app), evidence


def _headers() -> dict[str, str]:
    return {"X-Controller-Key": "controller-key"}


def test_health_and_every_mutating_route_require_controller_key(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=_headers()).json() == {
        "campaign_id": "evaluation-studio-v1",
        "state": "ready",
    }
    assert client.post(
        "/v1/scenarios/prepare",
        json={
            "scenario_id": "w2_empty_batch",
            "workflow_id": "workflow-2",
            "expected": {"run_status": "failed", "marker_count": 0, "reexecution_count": 0},
        },
    ).status_code == 401


def test_prepare_persists_sanitized_correlated_fixture_and_exact_w2_input(tmp_path: Path) -> None:
    client, evidence = _client(tmp_path)

    response = client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={
            "scenario_id": "w2_retrieval_miss",
            "workflow_id": "workflow-2",
            "expected": {
                "run_status": "completed",
                "marker_count": 0,
                "reexecution_count": 0,
                "partial_collection_count": 7,
            },
            "deterministic_provider_fault": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["input_payload"]["items"]) == 8
    assert body["input_payload"]["items"][0]["query"].startswith("investigation-negative-retrieval-miss-")
    assert set(body["correlation"]) == {"operation_id", "ui_action_id"}
    event = evidence.read_events()[-1]
    assert event["type"] == "scenario.prepared"
    assert event["correlation"] == body["correlation"]
    assert body["evidence"] == [f"events.ndjson#{event['event_id']}"]


def test_unsupported_barrier_fails_closed_and_records_blocked_evidence(tmp_path: Path) -> None:
    client, evidence = _client(tmp_path)
    prepared = client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={
            "scenario_id": "w2_child_pause_partial",
            "workflow_id": "workflow-2",
            "expected": {"run_status": "paused", "marker_count": 0, "reexecution_count": 0},
        },
    ).json()

    response = client.post(
        f"/v1/scenarios/{prepared['fixture_id']}/checkpoints/child_pause",
        headers=_headers(),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "runtime gateway is not configured; checkpoint is blocked"
    assert evidence.read_events()[-1]["type"] == "scenario.blocked"


def test_checkpoint_restart_verify_and_cleanup_use_authoritative_gateway(tmp_path: Path) -> None:
    gateway = _Gateway()
    client, evidence = _client(tmp_path, gateway=gateway)
    prepared = client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={
            "scenario_id": "w2_child_failure_partial",
            "workflow_id": "workflow-2",
            "expected": {
                "run_status": "completed",
                "marker_count": 0,
                "reexecution_count": 0,
                "partial_collection_count": 7,
            },
        },
    ).json()
    fixture_id = prepared["fixture_id"]

    checkpoint = client.post(
        f"/v1/scenarios/{fixture_id}/checkpoints/child_failure",
        headers=_headers(),
    )
    restart = client.get(
        f"/v1/scenarios/{fixture_id}/restart-status", headers=_headers()
    )
    verified = client.get(f"/v1/scenarios/{fixture_id}/verify", headers=_headers())
    cleaned = client.post(f"/v1/scenarios/{fixture_id}/cleanup", headers=_headers())

    assert checkpoint.status_code == 200
    assert restart.json()["state"] == "ready"
    assert verified.status_code == 200
    assert verified.json()["run_id"] == "run-123"
    assert verified.json()["partial_collection_count"] == 7
    assert verified.json()["correlation"]["operation_id"] == prepared["correlation"]["operation_id"]
    assert verified.json()["correlation"]["run_id"] == "run-123"
    assert cleaned.status_code == 200
    assert gateway.cleaned == [fixture_id]
    assert {event["type"] for event in evidence.read_events()} >= {
        "scenario.checkpointed",
        "scenario.restart-status.observed",
        "scenario.verified",
        "scenario.cleaned",
    }


def test_verify_rejects_gateway_result_without_exact_runtime_correlations(tmp_path: Path) -> None:
    class MissingCorrelationGateway(_Gateway):
        def verify(self, fixture):
            return {"run_status": "failed", "marker_count": 0, "reexecution_count": 0}

    client, evidence = _client(tmp_path, gateway=MissingCorrelationGateway())
    prepared = client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={
            "scenario_id": "w3_rejection",
            "workflow_id": "workflow-3",
            "expected": {"run_status": "failed", "marker_count": 0, "reexecution_count": 0},
        },
    ).json()

    response = client.get(
        f"/v1/scenarios/{prepared['fixture_id']}/verify", headers=_headers()
    )

    assert response.status_code == 409
    assert "correlation" in response.json()["detail"]
    assert evidence.read_events()[-1]["type"] == "scenario.blocked"


def test_prepare_rejects_unknown_scenarios_and_secret_shaped_workflow_ids(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    base = {
        "workflow_id": "workflow-2",
        "expected": {"run_status": "failed", "marker_count": 0, "reexecution_count": 0},
    }

    assert client.post(
        "/v1/scenarios/prepare", headers=_headers(), json={**base, "scenario_id": "unknown"}
    ).status_code == 422
    assert client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={**base, "scenario_id": "w2_empty_batch", "workflow_id": "sk-proj-" + "x" * 30},
    ).status_code == 422


def test_cleanup_disarms_an_unconsumed_one_shot_fault(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    prepared = client.post(
        "/v1/scenarios/prepare",
        headers=_headers(),
        json={
            "scenario_id": "w1_bad_credential",
            "workflow_id": "workflow-1",
            "expected": {"run_status": "failed", "marker_count": 0, "reexecution_count": 0},
            "deterministic_provider_fault": True,
        },
    ).json()

    response = client.post(
        f"/v1/scenarios/{prepared['fixture_id']}/cleanup", headers=_headers()
    )

    assert response.status_code == 200
    reopened = EvaluationFaultState(tmp_path / "artifacts" / "faults.sqlite3")
    assert reopened.consume(campaign_id="evaluation-studio-v1", target="provider") is None

from __future__ import annotations

import hashlib

import httpx
import pytest

from release.live_evaluation.scenario_controller import (
    LoopbackDeployment,
    LoopbackHttpScenarioRuntimeGateway,
    ScenarioFixture,
)


def _fixture(*, scenario: str = "w2_retrieval_miss") -> ScenarioFixture:
    return ScenarioFixture(
        fixture_id="fixture-123",
        scenario_id=scenario,
        workflow_id="workflow-2",
        expected={"run_status": "completed", "marker_count": 0, "reexecution_count": 0},
        input_payload={"items": []},
        operation_id="scenario-operation-123",
        ui_action_id="scenario-ui-123",
        marker_count_before=0,
        prepared_evidence="events.ndjson#prepared",
        baseline_run_ids=("run-old",),
    )


def test_gateway_snapshots_and_binds_exactly_one_new_scoped_run() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/admin/runs":
            calls = paths.count("/admin/runs")
            runs = [{"run_id": "run-old", "deployment_ref": "deployment-w2"}]
            if calls > 1:
                runs.append({"run_id": "run-new", "deployment_ref": "deployment-w2"})
            return httpx.Response(200, json={"runs": runs, "total": len(runs)})
        if request.url.path == "/v1/runs/run-new":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-new",
                    "deployment_ref": "deployment-w2",
                    "status": "succeeded",
                    "terminal_output": {
                        "results": [
                            {"index": index, "error": "retrieval_miss" if index == 3 else None}
                            for index in range(8)
                        ]
                    },
                },
            )
        if request.url.path == "/v1/deployments/deployment-w2/audits":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "audit-new",
                            "record_signature": "hmac-sha256:deadbeef",
                            "cost_event_id": "cost-new",
                            "execution_metadata": {},
                        }
                    ]
                },
            )
        if request.url.path == "/v1/runs/run-new/audit-verification":
            return httpx.Response(
                200,
                json={"verified": True, "signature_verified": True, "record_count": 1},
            )
        if request.url.path == "/v1/deployments/deployment-w2/cost":
            return httpx.Response(200, json={"total_cost_usd": 0.01})
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.snapshot_run_ids("workflow-2") == ("run-old",)
    result = gateway.verify(_fixture())

    assert result["run_id"] == "run-new"
    assert result["run_status"] == "completed"
    assert result["partial_collection_count"] == 7
    assert result["audit_event_id"] == "audit-new"
    assert result["cost_event_id"] == "cost-new"
    assert result["reexecution_count"] == 0


@pytest.mark.parametrize("new_runs", [[], ["run-a", "run-b"]])
def test_gateway_never_guesses_when_new_run_cardinality_is_not_one(new_runs: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/runs"
        runs = [
            {"run_id": run_id, "deployment_ref": "deployment-w2"}
            for run_id in ["run-old", *new_runs]
        ]
        return httpx.Response(200, json={"runs": runs, "total": len(runs)})

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://localhost:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="exactly one"):
        gateway.checkpoint(_fixture(), "run_submitted")


@pytest.mark.parametrize("checkpoint", ["refresh_before", "refresh_after"])
def test_workflow2_refresh_checkpoint_observes_the_bound_runtime_run(
    checkpoint: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-new",
                    "deployment_ref": "deployment-w2",
                    "status": "succeeded",
                },
            )
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert gateway.checkpoint(
        _fixture(scenario="w2_refresh_restoration"), checkpoint
    ) == {
        "state": "observed",
        "run_id": "run-new",
        "run_status": "succeeded",
    }


def test_gateway_maps_public_operator_cancellation_without_inventing_cancelled_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-new",
                    "deployment_ref": "deployment-w2",
                    "status": "failed",
                    "failure_state": {"reason": "operator_cancelled"},
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={"records": [{"audit_id": "audit-1", "record_signature": "sig"}]},
            )
        if request.url.path.endswith("/audit-verification"):
            return httpx.Response(200, json={"verified": True, "signature_verified": True})
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": 0})
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.verify(_fixture(scenario="w2_cancellation"))

    assert result["run_status"] == "cancelled"


def test_gateway_blocks_non_loopback_origins() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LoopbackHttpScenarioRuntimeGateway(
            campaign_id="evaluation-studio-v1",
            deployments={
                "workflow-2": LoopbackDeployment(
                    base_url="https://example.com", deployment_ref="deployment-w2"
                )
            },
        )


def test_restart_after_receipt_waits_for_exact_durable_barrier() -> None:
    restarts: list[tuple[str, str]] = []

    class Supervisor:
        def restart(self, *, deployment_ref: str, service_url: str) -> None:
            restarts.append((deployment_ref, service_url))

    class Barriers:
        def wait_for(self, *, campaign_id: str, run_id: str, timeout_seconds: float):
            assert campaign_id == "evaluation-studio-v1"
            assert run_id == "run-new"
            assert timeout_seconds == 10
            return {
                "run_id": run_id,
                "operation_key": "operation-new",
                "audit_id": "run-new:audit:3",
                "audit_digest": "a" * 64,
                "audit_signature_sha256": hashlib.sha256(b"hmac-sha256:signed").hexdigest(),
                "state": "waiting",
            }

        def mark_restarted(self, *, campaign_id: str, run_id: str) -> None:
            assert campaign_id == "evaluation-studio-v1"
            assert run_id == "run-new"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ]
                },
            )
        if request.url.path == "/v1/deployments/deployment-w2/audits":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "run-new:audit:3",
                            "run_id": "run-new",
                            "record_digest": "a" * 64,
                            "record_signature": "hmac-sha256:signed",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/runs/run-new/audit-verification":
            return httpx.Response(
                200, json={"verified": True, "signature_verified": True}
            )
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        supervisor=Supervisor(),
        receipt_barriers=Barriers(),
    )

    result = gateway.checkpoint(
        _fixture(scenario="w3_restart_after_receipt"), "restart_after_receipt_ready"
    )

    assert result == {
        "state": "restart_requested",
        "run_id": "run-new",
        "operation_key": "operation-new",
        "audit_id": "run-new:audit:3",
    }
    assert restarts == [("deployment-w2", "http://127.0.0.1:8122")]


def test_restart_after_receipt_fails_closed_without_barrier_store() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "runs": [
                    {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                    {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                ]
            },
        )

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        supervisor=type(
            "Supervisor", (), {"restart": lambda self, **_kwargs: None}
        )(),
    )

    with pytest.raises(NotImplementedError, match="barrier"):
        gateway.checkpoint(
            _fixture(scenario="w3_restart_after_receipt"),
            "restart_after_receipt_ready",
        )



def test_gateway_counts_seven_authoritative_completed_subgraphs_for_paused_parent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-new",
                    "deployment_ref": "deployment-w2",
                    "status": "paused_for_approval",
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": f"audit-{index}",
                            "record_signature": "sig",
                            "status": "completed",
                            "execution_metadata": {
                                "branch_index": index,
                                "subgraph_run_id": f"child-{index}",
                            },
                        }
                        for index in [0, 1, 2, 4, 5, 6, 7]
                    ]
                },
            )
        if request.url.path.endswith("/audit-verification"):
            return httpx.Response(200, json={"verified": True, "signature_verified": True})
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": 0.01})
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.verify(_fixture(scenario="w2_child_pause_partial"))

    assert result["run_status"] == "paused"
    assert result["partial_collection_count"] == 7


def test_gateway_proves_duplicate_approval_through_public_evidence_and_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new/evidence":
            return httpx.Response(
                200,
                json={
                    "approvals": [
                        {
                            "approval_id": "approval-1",
                            "run_id": "run-new",
                            "deployment_ref": "deployment-w2",
                            "status": "resolved",
                            "resolution": {"decision": "approve"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/approvals/approval-1/resolve"):
            return httpx.Response(409, json={"detail": "approval already resolved"})
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = gateway.checkpoint(
        _fixture(scenario="w3_duplicate_submission"), "duplicate_submission"
    )

    assert result == {
        "state": "duplicate_refused",
        "run_id": "run-new",
        "approval_id": "approval-1",
        "status_code": 409,
    }


def test_gateway_waits_for_authoritative_sla_rejection_then_fences_the_run() -> None:
    evidence_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal evidence_calls
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new/evidence":
            evidence_calls += 1
            approval = {
                "approval_id": "approval-1",
                "run_id": "run-new",
                "deployment_ref": "deployment-w2",
                "status": "pending",
                "sla_deadline": "2026-08-22T12:00:05Z",
                "resolution": None,
            }
            if evidence_calls > 1:
                approval.update(
                    {
                        "status": "resolved",
                        "resolution": {
                            "decision": "reject",
                            "actor": {"subject": "sla_enforcer"},
                        },
                    }
                )
            return httpx.Response(200, json={"approvals": [approval]})
        if request.url.path == "/admin/runs/run-new/cancel":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-new",
                    "deployment_ref": "deployment-w2",
                    "status": "failed",
                    "failure_state": {"reason": "operator_cancelled"},
                },
            )
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sla_poll_attempts=3,
        sla_poll_interval_seconds=0,
    )

    result = gateway.checkpoint(_fixture(scenario="w3_sla_expiry"), "advance_sla")

    assert result["state"] == "sla_expired"
    assert result["approval_id"] == "approval-1"
    assert result["run_id"] == "run-new"


def test_gateway_sla_poll_fails_closed_when_checker_never_resolves() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {"run_id": "run-old", "deployment_ref": "deployment-w2"},
                        {"run_id": "run-new", "deployment_ref": "deployment-w2"},
                    ],
                    "total": 2,
                },
            )
        if request.url.path == "/v1/runs/run-new/evidence":
            return httpx.Response(
                200,
                json={
                    "approvals": [
                        {
                            "approval_id": "approval-1",
                            "run_id": "run-new",
                            "deployment_ref": "deployment-w2",
                            "status": "pending",
                            "sla_deadline": "2026-08-22T12:00:05Z",
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id="evaluation-studio-v1",
        deployments={
            "workflow-2": LoopbackDeployment(
                base_url="http://127.0.0.1:8122", deployment_ref="deployment-w2"
            )
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sla_poll_attempts=2,
        sla_poll_interval_seconds=0,
    )

    with pytest.raises(RuntimeError, match="SLA checker"):
        gateway.checkpoint(_fixture(scenario="w3_sla_expiry"), "advance_sla")

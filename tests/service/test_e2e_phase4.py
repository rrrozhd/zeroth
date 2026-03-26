from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    CountingFinishRunner,
    approval_resume_graph,
    deploy_service,
    wait_for,
)
from zeroth.graph import GraphRepository
from zeroth.service.bootstrap import bootstrap_app


def test_phase4_end_to_end_deploy_invoke_resume_thread_and_rollback(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-phase4-e2e"),
        deployment_ref="phase4-e2e",
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        first_run = client.post("/runs", json={"input_payload": {"value": 3}})
        first_run_id = first_run.json()["run_id"]
        thread_id = first_run.json()["thread_id"]
        wait_for(
            lambda: client.get(f"/runs/{first_run_id}").json()["status"] == "paused_for_approval"
        )
        approval_id = client.get(f"/runs/{first_run_id}").json()["approval_paused_state"][
            "approval_id"
        ]

        input_contract = client.get(
            f"/deployments/{service.deployment.deployment_ref}/input-contract"
        )
        metadata = client.get(f"/deployments/{service.deployment.deployment_ref}/metadata")
        first_resolution = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve", "approver": "reviewer-1"},
        )

        second_run = client.post(
            "/runs",
            json={"input_payload": {"value": 5}, "thread_id": thread_id},
        )
        second_run_id = second_run.json()["run_id"]
        wait_for(
            lambda: client.get(f"/runs/{second_run_id}").json()["status"] == "paused_for_approval"
        )
        second_approval_id = client.get(f"/runs/{second_run_id}").json()["approval_paused_state"][
            "approval_id"
        ]
        second_resolution = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{second_approval_id}/resolve",
            json={
                "decision": "edit_and_approve",
                "approver": "reviewer-1",
                "edited_payload": {"value": 9},
            },
        )

    assert input_contract.status_code == 200
    assert metadata.status_code == 200
    assert metadata.json()["graph_version"] == 1
    assert first_resolution.status_code == 200
    assert first_resolution.json()["run"]["status"] == "succeeded"
    assert first_resolution.json()["run"]["terminal_output"] == {"value": 4}
    assert second_run.status_code == 202
    assert second_run.json()["thread_id"] == thread_id
    assert second_resolution.status_code == 200
    assert second_resolution.json()["run"]["status"] == "succeeded"
    assert second_resolution.json()["run"]["terminal_output"] == {"value": 10}

    graph_repository = GraphRepository(sqlite_db)
    draft_v2 = graph_repository.clone_published_to_draft(service.deployment.graph_id, 1)
    graph_repository.save(draft_v2)
    published_v2 = graph_repository.publish(draft_v2.graph_id, draft_v2.version)
    service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )

    v2_app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    v2_app.state.bootstrap.orchestrator.agent_runners["finish-step"] = finish_runner
    with TestClient(v2_app) as client:
        v2_metadata = client.get(f"/deployments/{service.deployment.deployment_ref}/metadata")
        stale_thread = client.post(
            "/runs",
            json={"input_payload": {"value": 7}, "thread_id": thread_id},
        )

    assert v2_metadata.status_code == 200
    assert v2_metadata.json()["graph_version"] == 2
    assert stale_thread.status_code == 409

    rolled_back = service.deployment_service.rollback(
        service.deployment.deployment_ref,
        target_graph_version=1,
    )
    rollback_app = bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    rollback_app.state.bootstrap.orchestrator.agent_runners["finish-step"] = finish_runner

    with TestClient(rollback_app) as client:
        rollback_metadata = client.get(
            f"/deployments/{service.deployment.deployment_ref}/metadata"
        )
        rollback_run = client.post("/runs", json={"input_payload": {"value": 2}})
        rollback_run_id = rollback_run.json()["run_id"]
        wait_for(
            lambda: client.get(f"/runs/{rollback_run_id}").json()["status"]
            == "paused_for_approval"
        )
        rollback_approval_id = client.get(f"/runs/{rollback_run_id}").json()[
            "approval_paused_state"
        ]["approval_id"]
        rollback_resolution = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{rollback_approval_id}/resolve",
            json={"decision": "approve", "approver": "reviewer-1"},
        )

    assert rolled_back.graph_version == 1
    assert rollback_metadata.status_code == 200
    assert rollback_metadata.json()["graph_version"] == 1
    assert rollback_metadata.json()["deployment_version"] == rolled_back.version
    assert rollback_resolution.status_code == 200
    assert rollback_resolution.json()["run"]["status"] == "succeeded"
    assert rollback_resolution.json()["run"]["terminal_output"] == {"value": 3}

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.service.helpers import (
    CountingFinishRunner,
    approval_graph,
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    wait_for,
)
from zeroth.contracts.graph import GraphRepository
from zeroth.contracts.graph import (
    DisplayMetadata,
    ExecutionSettings,
    Graph,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.bootstrap.factory import bootstrap_scoped_service
from zeroth.service.bootstrap import bootstrap_app

#: Statuses a run can still leave on its own. Anything else is terminal.
_NON_TERMINAL = frozenset({"queued", "running", "paused_for_approval", "waiting_interrupt"})


def _is_terminal(client, run_id: str) -> bool:
    """True once the run has settled, whatever it settled on."""
    status = client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
    return status not in _NON_TERMINAL


def _describe(client, run_id: str, phase: str):
    """What the run looked like when a wait gave up — see ZER21-AUD-001."""

    def describe() -> str:
        body = client.get(f"/runs/{run_id}", headers=operator_headers()).json()
        return (
            f"phase={phase!r} run {run_id} status={body.get('status')!r} "
            f"failure_state={body.get('failure_state')!r}"
        )

    return describe


def test_approval_wait_description_identifies_phase() -> None:
    client = Mock()
    client.get.return_value.json.return_value = {
        "status": "running",
        "failure_state": None,
    }

    assert "phase='approval_pause'" in _describe(client, "run-1", "approval_pause")()
    assert "phase='terminal_resume'" in _describe(client, "run-1", "terminal_resume")()


async def test_approval_api_queries_pending_approvals_by_id_run_thread_and_scope(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, approval_graph(graph_id="graph-approval-query"))
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )

        paused_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()
        approval_id = paused_payload["approval_paused_state"]["approval_id"]
        thread_id = paused_payload["thread_id"]

        all_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            headers=operator_headers(),
        )
        by_run_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"run_id": run_id},
            headers=operator_headers(),
        )
        by_thread_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"thread_id": thread_id},
            headers=operator_headers(),
        )
        by_id_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}",
            headers=operator_headers(),
        )
        query_by_id_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"approval_id": approval_id},
            headers=operator_headers(),
        )

    assert all_response.status_code == 200
    assert [item["approval_id"] for item in all_response.json()] == [approval_id]
    assert [item["approval_id"] for item in by_run_response.json()] == [approval_id]
    assert [item["approval_id"] for item in by_thread_response.json()] == [approval_id]
    assert by_id_response.status_code == 200
    assert by_id_response.json()["approval_id"] == approval_id
    assert [item["approval_id"] for item in query_by_id_response.json()] == [approval_id]

    graph_repository = GraphRepository(sqlite_db)
    draft = await graph_repository.clone_published_to_draft(
        service.deployment.graph_id,
        service.deployment.graph_version,
    )
    await graph_repository.save(draft)
    published_v2 = await graph_repository.publish(draft.graph_id, draft.version)
    await service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )
    fresh_app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )

    with TestClient(fresh_app) as client:
        missing_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}",
            headers=operator_headers(),
        )

    assert missing_response.status_code == 404
    assert missing_response.json() == {"detail": "approval not found"}


async def test_parent_approval_api_discovers_and_resumes_child_gate(sqlite_db) -> None:
    """The served parent authorizes its real child approval without changing provenance."""
    child_service, _ = await deploy_service(
        sqlite_db,
        approval_graph(graph_id="approval-api-child-graph"),
        deployment_ref="approval-api-child",
    )
    parent_graph = Graph(
        graph_id="approval-api-parent-graph",
        name="Approval API parent graph",
        version=1,
        entry_step="child",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            SubgraphNode(
                node_id="child",
                graph_version_ref="approval-api-parent-graph@1",
                display=DisplayMetadata(title="Approval child"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                subgraph=SubgraphNodeData(graph_ref="approval-api-child"),
            )
        ],
        edges=[],
    )
    graph_repository = GraphRepository(sqlite_db)
    parent_graph = await graph_repository.create(parent_graph)
    parent_graph = await graph_repository.publish(parent_graph.graph_id, parent_graph.version)
    parent_deployment = await child_service.deployment_service.deploy(
        "approval-api-parent",
        parent_graph.graph_id,
        parent_graph.version,
    )
    service = await bootstrap_scoped_service(
        sqlite_db,
        deployment_ref=parent_deployment.deployment_ref,
        auth_config=child_service.auth_config,
    )
    signer = EnvHmacSigner(
        key_id="approval-api-child-continuation",
        keys={"approval-api-child-continuation": b"test-key"},
    )
    service.audit_repository._signer = signer  # noqa: SLF001 - test wiring seam
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=parent_deployment.deployment_ref,
        auth_config=child_service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_id = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        ).json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "child_approval_pause"),
        )

        pending = client.get(
            f"/deployments/{parent_deployment.deployment_ref}/approvals",
            headers=operator_headers(),
        )
        assert pending.status_code == 200
        [approval] = pending.json()
        assert approval["deployment_ref"] == "approval-api-child"
        assert approval["graph_version_ref"].startswith("approval-api-child-graph:v")

        response = client.post(
            (
                f"/deployments/{parent_deployment.deployment_ref}/approvals/"
                f"{approval['approval_id']}/resolve"
            ),
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )
        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "child_approval_resume"),
        )
        settled = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert response.status_code == 200
    assert response.json()["approval"]["run_id"] != run_id
    assert response.json()["run"]["run_id"] == run_id
    assert response.json()["run"]["status"] == "queued"
    assert settled["status"] == "succeeded"

    child_runs = await service.run_repository.list_child_runs(run_id)
    assert len(child_runs) == 1
    assert child_runs[0].deployment_ref == "approval-api-child"
    assert child_runs[0].status.value == "COMPLETED"


@pytest.mark.parametrize(
    (
        "decision",
        "edited_payload",
        "expected_response_status",
        "expected_status",
        "expected_output",
    ),
    [
        ("approve", None, "queued", "succeeded", {"value": 4}),
        ("reject", None, "failed", "failed", None),
        ("edit_and_approve", {"value": 4}, "queued", "succeeded", {"value": 5}),
    ],
)
async def test_approval_api_resolves_all_decisions_and_resumes_when_appropriate(
    sqlite_db,
    decision,
    edited_payload,
    expected_response_status,
    expected_status,
    expected_output,
) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id=f"graph-approval-{decision}"),
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        payload = {"decision": decision}
        if edited_payload is not None:
            payload["edited_payload"] = edited_payload

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        # Durable approvals return the queued continuation immediately. Rejection is
        # terminal at resolution time and preserves that state in the response.
        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        run_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert response.status_code == 200
    assert response.json()["approval"]["resolution"]["decision"] == decision
    assert response.json()["run"]["run_id"] == run_id
    assert response.json()["run"]["status"] == expected_response_status
    assert run_payload["status"] == expected_status
    assert run_payload["terminal_output"] == expected_output
    if decision == "reject":
        assert finish_runner.call_count == 0
        assert run_payload["failure_state"]["reason"] == "approval_rejected"
    else:
        assert finish_runner.call_count == 1
        assert finish_runner.last_input == (edited_payload or {"value": 3})


async def test_approval_api_duplicate_resolution_is_idempotent(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-approval-idempotent"),
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    schedule_continuation = AsyncMock(wraps=service.approval_service.schedule_continuation)
    service.approval_service.schedule_continuation = schedule_continuation
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        payload = {
            "decision": "edit_and_approve",
            "edited_payload": {"value": 8},
        }
        first_response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        second_response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        # A replay may observe queued, running, or terminal state, but it must not
        # schedule the already-resolved approval a second time.
        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        run_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["approval"] == second_response.json()["approval"]
    assert schedule_continuation.await_count == 1
    assert run_payload["status"] == "succeeded"
    assert run_payload["terminal_output"] == {"value": 9}
    assert finish_runner.call_count == 1


async def test_durable_resolve_returns_queued_then_clients_poll_completion(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-approval-contract")
    )
    service.orchestrator.agent_runners["finish-step"] = CountingFinishRunner()
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_id = client.post(
            "/runs", json={"input_payload": {"value": 3}}, headers=operator_headers()
        ).json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(f"/runs/{run_id}", headers=operator_headers()).json()[
            "approval_paused_state"
        ]["approval_id"]

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )

        assert response.status_code == 200
        assert response.json()["approval"]["resolution"]["decision"] == "approve"
        assert response.json()["run"]["status"] == "queued"

        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        settled = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert settled["status"] == "succeeded"
    assert settled["terminal_output"] == {"value": 4}


async def test_resolve_without_durable_worker_remains_synchronous(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-approval-inline-contract")
    )
    service.orchestrator.agent_runners["finish-step"] = CountingFinishRunner()
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_id = client.post(
            "/runs", json={"input_payload": {"value": 3}}, headers=operator_headers()
        ).json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(f"/runs/{run_id}", headers=operator_headers()).json()[
            "approval_paused_state"
        ]["approval_id"]
        service.worker = None

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "succeeded"
    assert response.json()["run"]["terminal_output"] == {"value": 4}

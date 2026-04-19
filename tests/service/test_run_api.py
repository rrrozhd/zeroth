from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from tests.service.helpers import (
    BlockingAgentRunner,
    FailingAgentRunner,
    RunInputPayloadV2,
    agent_graph,
    approval_graph,
    deploy_service,
    operator_headers,
    wait_for,
)
from zeroth.core.graph import GraphRepository
from zeroth.core.runs import Run, RunFailureState, RunStatus
from zeroth.core.service.bootstrap import bootstrap_app
from zeroth.core.service.run_api import RunInvocationRequest, RunStatusResponse


async def test_run_creation_accepts_input_and_supplied_thread_id(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-create"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": "thread-continue"},
            headers=operator_headers(),
        )
        payload = response.json()
        run_id = payload["run_id"]

        assert response.status_code == 202
        assert payload["deployment_ref"] == service.deployment.deployment_ref
        assert payload["thread_id"] == "thread-continue"
        assert payload["status"] == "queued"
        assert run_id

        wait_for(started.is_set)
        release.set()


async def test_run_creation_without_thread_id_returns_new_thread_linkage(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-new-thread"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        payload = response.json()

        assert response.status_code == 202
        assert payload["status"] == "queued"
        assert payload["thread_id"] == payload["run_id"]

        wait_for(started.is_set)
        release.set()


async def test_run_creation_rejects_invalid_input(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-invalid"))
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": "not-an-int"}},
            headers=operator_headers(),
        )

    assert response.status_code == 422
    assert "value" in response.text


async def test_run_creation_validates_against_deployed_input_contract_version(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-pinned-contract"))
    await service.contract_registry.register(
        RunInputPayloadV2,
        name="contract://input",
        version=2,
    )
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )

        assert response.status_code == 202
        wait_for(started.is_set)
        release.set()


async def test_run_creation_rejects_foreign_thread_id(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-thread-mismatch"))
    await service.run_repository.create(
        Run(
            run_id="foreign-run",
            thread_id="shared-thread",
            graph_version_ref="other-graph:v1",
            deployment_ref="other-deployment",
        )
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}, "thread_id": "shared-thread"},
            headers=operator_headers(),
        )

    assert response.status_code == 409
    assert "thread identity mismatch" in response.text


async def test_run_status_reports_running_and_completed_state(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-complete"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 42},
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]

        wait_for(started.is_set)

        running_response = client.get(f"/runs/{run_id}", headers=operator_headers())
        running_payload = running_response.json()

        assert running_response.status_code == 200
        assert running_payload["status"] == "running"
        assert running_payload["deployment_ref"] == service.deployment.deployment_ref
        assert running_payload["thread_id"] == run_id

        release.set()
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "succeeded"
            )
        )
        completed_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert completed_payload["status"] == "succeeded"
    assert completed_payload["terminal_output"] == {"value": 42}
    assert completed_payload["failure_state"] is None
    assert completed_payload["submitted_by"]["subject"] == "operator-1"
    assert completed_payload["tenant_id"] == "default"
    assert completed_payload["workspace_id"] is None


async def test_run_status_returns_404_for_unknown_run(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-missing"))
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/runs/missing-run", headers=operator_headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


async def test_run_status_does_not_expose_runs_from_other_deployments(sqlite_db) -> None:
    first_service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-scope-a"))
    second_service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-run-scope-b"),
        deployment_ref="service-run-api-b",
    )
    foreign_run = await second_service.run_repository.create(
        Run(
            graph_version_ref=second_service.deployment.graph_version_ref,
            deployment_ref=second_service.deployment.deployment_ref,
        )
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=first_service.deployment.deployment_ref,
        auth_config=first_service.auth_config,
    )
    app.state.bootstrap = first_service

    with TestClient(app) as client:
        response = client.get(f"/runs/{foreign_run.run_id}", headers=operator_headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


async def test_run_status_does_not_expose_runs_from_other_deployment_versions(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-scope-version"))
    original_run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
        )
    )

    graph_repository = GraphRepository(sqlite_db)
    draft = await graph_repository.clone_published_to_draft("graph-run-scope-version", 1)
    await graph_repository.save(draft)
    published_v2 = await graph_repository.publish(draft.graph_id, draft.version)
    await service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )

    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )

    with TestClient(app) as client:
        response = client.get(f"/runs/{original_run.run_id}", headers=operator_headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


async def test_run_status_reports_failed_state(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-failed"))
    started = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = FailingAgentRunner(started=started)
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]

        wait_for(started.is_set)
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "failed"
            )
        )
        failed_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert failed_payload["status"] == "failed"
    assert failed_payload["terminal_output"] is None
    assert failed_payload["failure_state"]["reason"] == "node_execution_failed"
    assert failed_payload["failure_state"]["message"] == "boom"


async def test_run_creation_returns_404_when_pinned_input_contract_is_missing(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-missing-contract"))
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            """
            UPDATE deployment_versions
            SET entry_input_contract_version = 999
            WHERE deployment_id = ?
            """,
            (service.deployment.deployment_id,),
        )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "deployment input contract 'contract://input' version 999 not found"
    }


async def test_run_status_reports_approval_paused_state(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, approval_graph(graph_id="graph-run-paused"))
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
            )
        )
        paused_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert paused_payload["status"] == "paused_for_approval"
    assert paused_payload["approval_paused_state"]["node_id"] == "approval-step"
    assert paused_payload["approval_paused_state"]["approval_id"] is not None
    assert paused_payload["terminal_output"] is None


async def test_run_status_reports_policy_and_loop_guard_termination(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-run-terminated"))
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    policy_run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
            failure_state=RunFailureState(reason="policy_violation", message="denied"),
        ).model_copy(update={"status": RunStatus.FAILED})
    )
    loop_guard_run = await service.run_repository.create(
        Run(
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
            failure_state=RunFailureState(reason="max_total_steps", message="too many"),
        ).model_copy(update={"status": RunStatus.FAILED})
    )

    with TestClient(app) as client:
        policy_payload = client.get(f"/runs/{policy_run.run_id}", headers=operator_headers()).json()
        loop_guard_payload = client.get(
            f"/runs/{loop_guard_run.run_id}",
            headers=operator_headers(),
        ).json()

    assert policy_payload["status"] == "terminated_by_policy"
    assert loop_guard_payload["status"] == "terminated_by_loop_guard"


def test_run_api_models_validate_expected_shapes() -> None:
    invocation = RunInvocationRequest.model_validate(
        {"input_payload": {"value": 5}, "thread_id": "thread-1"}
    )
    status_payload = RunStatusResponse.model_validate(
        {
            "run_id": "run-1",
            "status": "queued",
            "deployment_ref": "deployment-1",
            "graph_version_ref": "graph-1:1",
            "thread_id": "thread-1",
            "current_step": None,
            "terminal_output": None,
            "failure_state": None,
            "approval_paused_state": None,
            "audit_refs": [],
        }
    )

    assert invocation.input_payload == {"value": 5}
    assert invocation.thread_id == "thread-1"
    assert status_payload.status.value == "queued"

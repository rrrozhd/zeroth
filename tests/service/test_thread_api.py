from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from tests.service.helpers import (
    BlockingAgentRunner,
    agent_graph,
    bootstrap_only_app,
    build_run_for_service,
    deploy_service,
    operator_headers,
    service_app,
    wait_for,
)
from zeroth.core.graph import GraphRepository


async def test_thread_api_creates_new_thread_when_thread_id_is_omitted(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-new"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        payload = response.json()

        assert response.status_code == 202
        assert payload["thread_id"] == payload["run_id"]
        wait_for(started.is_set)
        release.set()


async def test_thread_api_continues_existing_thread_for_same_deployment_snapshot(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-continue"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        first = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        thread_id = first.json()["thread_id"]
        run_id = first.json()["run_id"]
        wait_for(started.is_set)
        release.set()
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "succeeded"
            )
        )

        second = client.post(
            "/runs",
            json={"input_payload": {"value": 4}, "thread_id": thread_id},
            headers=operator_headers(),
        )

    assert second.status_code == 202
    assert second.json()["thread_id"] == thread_id


async def test_thread_api_accepts_new_explicit_thread_id_as_fresh_context(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-missing"))
    web_app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": "missing-thread"},
            headers=operator_headers(),
        )

    assert response.status_code == 202
    assert response.json()["thread_id"] == "missing-thread"


async def test_thread_api_rejects_thread_from_other_deployment(sqlite_db) -> None:
    first_service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-a"))
    second_service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-thread-b"),
        deployment_ref="thread-service-b",
    )
    second_run = await second_service.run_repository.create(build_run_for_service(second_service))
    web_app = await service_app(sqlite_db, first_service.deployment.deployment_ref, first_service)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": second_run.thread_id},
            headers=operator_headers(),
        )

    assert response.status_code == 409
    assert "thread identity mismatch" in response.json()["detail"]


async def test_thread_api_rejects_thread_from_other_deployment_version(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-version"))
    first_run = await service.run_repository.create(build_run_for_service(service))

    graph_repository = GraphRepository(sqlite_db)
    draft = await graph_repository.clone_published_to_draft("graph-thread-version", 1)
    await graph_repository.save(draft)
    published_v2 = await graph_repository.publish(draft.graph_id, draft.version)
    await service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )
    web_app = await bootstrap_only_app(sqlite_db, service.deployment.deployment_ref)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": first_run.thread_id},
            headers=operator_headers(),
        )

    assert response.status_code == 409
    assert "thread identity mismatch" in response.json()["detail"]


async def test_thread_api_keeps_thread_linkage_visible_in_run_state_and_audits(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-audits"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        thread_id = create_response.json()["thread_id"]
        wait_for(started.is_set)
        release.set()
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "succeeded"
            )
        )
        run_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    audits = await service.audit_repository.list_by_thread(thread_id)

    assert run_payload["thread_id"] == thread_id
    assert run_payload["audit_refs"]
    assert audits
    assert all(record.thread_id == thread_id for record in audits)

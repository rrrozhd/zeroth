from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from tests.service.helpers import (
    BlockingAgentRunner,
    agent_graph,
    bootstrap_only_app,
    build_run_for_service,
    deploy_service,
    service_app,
    wait_for,
)
from zeroth.graph import GraphRepository


def test_thread_api_creates_new_thread_when_thread_id_is_omitted(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-new"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        response = client.post("/runs", json={"input_payload": {"value": 3}})
        payload = response.json()

        assert response.status_code == 202
        assert payload["thread_id"] == payload["run_id"]
        wait_for(started.is_set)
        release.set()


def test_thread_api_continues_existing_thread_for_same_deployment_snapshot(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-continue"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        first = client.post("/runs", json={"input_payload": {"value": 3}})
        thread_id = first.json()["thread_id"]
        run_id = first.json()["run_id"]
        wait_for(started.is_set)
        release.set()
        wait_for(lambda: client.get(f"/runs/{run_id}").json()["status"] == "succeeded")

        second = client.post(
            "/runs",
            json={"input_payload": {"value": 4}, "thread_id": thread_id},
        )

    assert second.status_code == 202
    assert second.json()["thread_id"] == thread_id


def test_thread_api_accepts_new_explicit_thread_id_as_fresh_context(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-missing"))
    web_app = service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": "missing-thread"},
        )

    assert response.status_code == 202
    assert response.json()["thread_id"] == "missing-thread"


def test_thread_api_rejects_thread_from_other_deployment(sqlite_db) -> None:
    first_service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-a"))
    second_service, _ = deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-thread-b"),
        deployment_ref="thread-service-b",
    )
    second_run = second_service.run_repository.create(build_run_for_service(second_service))
    web_app = service_app(sqlite_db, first_service.deployment.deployment_ref, first_service)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": second_run.thread_id},
        )

    assert response.status_code == 409
    assert "thread identity mismatch" in response.json()["detail"]


def test_thread_api_rejects_thread_from_other_deployment_version(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-version"))
    first_run = service.run_repository.create(build_run_for_service(service))

    graph_repository = GraphRepository(sqlite_db)
    draft = graph_repository.clone_published_to_draft("graph-thread-version", 1)
    graph_repository.save(draft)
    published_v2 = graph_repository.publish(draft.graph_id, draft.version)
    service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )
    web_app = bootstrap_only_app(sqlite_db, service.deployment.deployment_ref)

    with TestClient(web_app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}, "thread_id": first_run.thread_id},
        )

    assert response.status_code == 409
    assert "thread identity mismatch" in response.json()["detail"]


def test_thread_api_keeps_thread_linkage_visible_in_run_state_and_audits(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, agent_graph(graph_id="graph-thread-audits"))
    started = threading.Event()
    release = threading.Event()
    service.orchestrator.agent_runners["agent-step"] = BlockingAgentRunner(
        started=started,
        release=release,
        output_data={"value": 10},
    )
    web_app = service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(web_app) as client:
        create_response = client.post("/runs", json={"input_payload": {"value": 3}})
        run_id = create_response.json()["run_id"]
        thread_id = create_response.json()["thread_id"]
        wait_for(started.is_set)
        release.set()
        wait_for(lambda: client.get(f"/runs/{run_id}").json()["status"] == "succeeded")
        run_payload = client.get(f"/runs/{run_id}").json()

    audits = service.audit_repository.list_by_thread(thread_id)

    assert run_payload["thread_id"] == thread_id
    assert run_payload["audit_refs"]
    assert audits
    assert all(record.thread_id == thread_id for record in audits)

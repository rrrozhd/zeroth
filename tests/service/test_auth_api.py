from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    wait_for,
)
from zeroth.service.bootstrap import bootstrap_app


def test_service_health_requires_authentication(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, approval_resume_graph(graph_id="graph-auth-health"))
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_service_health_accepts_api_key_authentication(sqlite_db) -> None:
    service, _ = deploy_service(sqlite_db, approval_resume_graph(graph_id="graph-auth-health-key"))
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/health", headers=operator_headers())

    assert response.status_code == 200
    assert response.json()["deployment_ref"] == service.deployment.deployment_ref


def test_approval_resolution_uses_authenticated_principal(sqlite_db) -> None:
    service, _ = deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-auth-approval"),
    )
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: client.get(
                f"/runs/{run_id}",
                headers=operator_headers(),
            ).json()["status"]
            == "paused_for_approval"
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )

    assert response.status_code == 200
    assert response.json()["approval"]["resolution"]["actor"]["subject"] == "reviewer-1"

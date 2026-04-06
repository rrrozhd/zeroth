from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    wait_for,
)
from zeroth.service.bootstrap import bootstrap_app


async def test_reviewer_cannot_create_runs(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, approval_resume_graph(graph_id="graph-rbac-run-create"))
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=reviewer_headers(),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


async def test_operator_cannot_resolve_approvals(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-rbac-approval-resolve"),
    )
    app = await bootstrap_app(
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
            headers=operator_headers(),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


async def test_admin_can_read_deployment_metadata(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, approval_resume_graph(graph_id="graph-rbac-metadata"))
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/metadata",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    assert response.json()["deployment_ref"] == service.deployment.deployment_ref

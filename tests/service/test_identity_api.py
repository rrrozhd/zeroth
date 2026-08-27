from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, deploy_service, operator_headers
from zeroth.service.bootstrap import bootstrap_app


async def test_identity_reports_authenticated_scope_and_roles(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-identity"),
        deployment_ref="identity-test",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/v1/identity", headers=operator_headers())

    assert response.status_code == 200
    assert response.json() == {
        "subject": "operator-1",
        "roles": ["operator"],
        "tenant_id": "default",
        "workspace_id": None,
    }


async def test_identity_requires_authentication(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-identity-auth"),
        deployment_ref="identity-auth-test",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get("/v1/identity")

    assert response.status_code == 401

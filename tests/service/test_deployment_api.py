"""Tests for the GET /v1/deployments listing endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, deploy_service, operator_headers
from zeroth.core.service.bootstrap import bootstrap_app

DEPLOYMENT = "deployments-test"


async def test_list_deployments_returns_serving_deployment(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-deployments"),
        deployment_ref=DEPLOYMENT,
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/deployments", headers=operator_headers())

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["deployment_ref"] == DEPLOYMENT
    assert entry["graph_version_ref"] == service.deployment.graph_version_ref
    assert entry["status"] == "active"
    assert entry["serving"] is True
    assert entry["created_at"]


async def test_list_deployments_is_tenant_scoped(sqlite_db) -> None:
    """G4: the listing is scoped to the serving deployment's tenant.

    A deployment persisted under a different tenant on the SAME store must not
    leak into another tenant's listing.
    """
    # Serving deployment belongs to tenant "default".
    service, serving = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-tenant-a"),
        deployment_ref="deploy-tenant-a",
        tenant_id="default",
    )
    # A second deployment persisted under a DIFFERENT tenant on the shared store.
    await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-tenant-b"),
        deployment_ref="deploy-tenant-b",
        tenant_id="tenant-b",
    )

    app = await bootstrap_app(sqlite_db, deployment_ref=serving.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/deployments", headers=operator_headers())

    assert r.status_code == 200
    refs = {entry["deployment_ref"] for entry in r.json()}
    # Only the serving tenant's deployment is visible; tenant-b's is filtered out.
    assert refs == {"deploy-tenant-a"}


async def test_list_deployments_requires_auth(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-deployments-auth"),
        deployment_ref=DEPLOYMENT + "-auth",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/deployments")

    assert r.status_code == 401

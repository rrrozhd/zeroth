"""Tests for the GET /v1/deployments listing endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)
from zeroth.governance.identity import ServiceRole
from zeroth.service.bootstrap.factory import bootstrap_scoped_app as bootstrap_app
from zeroth.service.deployments.repository import SQLiteDeploymentRepository

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


async def test_list_deployments_uses_principal_scope_without_serving_deployment(
    sqlite_db,
) -> None:
    auth_config = scoped_auth_config(
        ("workspace-a", "workspace-a-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
    )
    _, owned = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-no-serving-a"),
        deployment_ref="deploy-no-serving-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        auth_config=auth_config,
    )
    await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-no-serving-b"),
        deployment_ref="deploy-no-serving-b",
        tenant_id="tenant-b",
        workspace_id="workspace-b",
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=owned.deployment_ref,
        tenant_id=owned.tenant_id,
        workspace_id=owned.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap.deployment = None

    with TestClient(app) as client:
        response = client.get("/v1/deployments", headers=api_key_headers("workspace-a-key"))

    assert response.status_code == 200
    assert {entry["deployment_ref"] for entry in response.json()} == {"deploy-no-serving-a"}


async def test_list_deployments_is_exactly_workspace_scoped(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("workspace-a", "workspace-a-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("workspace-b", "workspace-b-key", ServiceRole.OPERATOR, "tenant-a", "workspace-b"),
        ("workspace-null", "workspace-null-key", ServiceRole.OPERATOR, "tenant-a", None),
    )
    _, serving = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-workspace-a"),
        deployment_ref="deploy-workspace-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        auth_config=auth_config,
    )
    await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-workspace-b"),
        deployment_ref="deploy-workspace-b",
        tenant_id="tenant-a",
        workspace_id="workspace-b",
        auth_config=auth_config,
    )
    await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-workspace-null"),
        deployment_ref="deploy-workspace-null",
        tenant_id="tenant-a",
        workspace_id=None,
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=serving.deployment_ref,
        tenant_id=serving.tenant_id,
        workspace_id=serving.workspace_id,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        workspace_a = client.get("/v1/deployments", headers=api_key_headers("workspace-a-key"))
        workspace_b = client.get("/v1/deployments", headers=api_key_headers("workspace-b-key"))
        workspace_null = client.get(
            "/v1/deployments", headers=api_key_headers("workspace-null-key")
        )

    assert {entry["deployment_ref"] for entry in workspace_a.json()} == {"deploy-workspace-a"}
    assert {entry["deployment_ref"] for entry in workspace_b.json()} == {"deploy-workspace-b"}
    assert {entry["deployment_ref"] for entry in workspace_null.json()} == {"deploy-workspace-null"}


async def test_create_deployment_hides_foreign_workspace_graph(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("workspace-a", "workspace-a-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("workspace-b", "workspace-b-key", ServiceRole.OPERATOR, "tenant-a", "workspace-b"),
    )
    _, serving = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-foreign-create"),
        deployment_ref="deploy-foreign-create-serving",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=serving.deployment_ref,
        tenant_id=serving.tenant_id,
        workspace_id=serving.workspace_id,
        auth_config=auth_config,
    )
    payload = {
        "deployment_ref": "deploy-foreign-create-attempt",
        "graph_id": "graph-foreign-create",
        "graph_version": 1,
    }

    with TestClient(app) as client:
        foreign = client.post(
            "/v1/deployments",
            json=payload,
            headers=api_key_headers("workspace-b-key"),
        )
        owned = client.post(
            "/v1/deployments",
            json=payload | {"deployment_ref": "deploy-owned-create"},
            headers=api_key_headers("workspace-a-key"),
        )

    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "graph version not found"}
    assert owned.status_code == 201
    repository = SQLiteDeploymentRepository(sqlite_db)
    assert (
        await repository.get(
            "deploy-owned-create", tenant_id="tenant-a", workspace_id="workspace-a"
        )
        is not None
    )
    assert (
        await repository.get(
            "deploy-foreign-create-attempt",
            tenant_id="tenant-a",
            workspace_id="workspace-b",
        )
        is None
    )


async def test_rollback_deployment_hides_foreign_workspace_ref(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("workspace-a", "workspace-a-key", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("workspace-b", "workspace-b-key", ServiceRole.OPERATOR, "tenant-a", "workspace-b"),
    )
    _, serving = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-foreign-rollback"),
        deployment_ref="deploy-foreign-rollback",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=serving.deployment_ref,
        tenant_id=serving.tenant_id,
        workspace_id=serving.workspace_id,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        foreign = client.post(
            "/v1/deployments/deploy-foreign-rollback/rollback",
            json={"target_graph_version": 1},
            headers=api_key_headers("workspace-b-key"),
        )
        owned = client.post(
            "/v1/deployments/deploy-foreign-rollback/rollback",
            json={"target_graph_version": 1},
            headers=api_key_headers("workspace-a-key"),
        )

    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "deployment not found"}
    assert owned.status_code == 201
    assert owned.json()["version"] == 2


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

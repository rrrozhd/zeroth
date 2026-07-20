"""Studio workflows are isolated by the principal's exact tenant/workspace scope."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.contracts.graph.models import Graph
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.service.api.studio_api import router as studio_router


def _studio_app(
    repo: GraphRepository,
    *,
    tenant_id: str = "tenant-a",
    workspace_id: str | None,
) -> FastAPI:
    app = FastAPI()
    bootstrap = type("Bootstrap", (), {})()
    bootstrap.graph_repository = repo
    bootstrap.audit_repository = None
    app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject=f"{tenant_id}:{workspace_id}:admin",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.ADMIN],
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(studio_router)
    return app


def _create(client: TestClient, name: str) -> str:
    response = client.post("/api/studio/v1/workflows", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_studio_stamps_scope_and_owner_lifecycle_still_works(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    app = _studio_app(repo, workspace_id="workspace-a")

    with TestClient(app) as client:
        graph_id = _create(client, "Owner flow")

        persisted = await repo.get(
            graph_id,
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
        assert persisted is not None
        assert persisted.tenant_id == "tenant-a"
        assert persisted.workspace_id == "workspace-a"

        assert graph_id in {item["id"] for item in client.get("/api/studio/v1/workflows").json()}
        assert client.get(f"/api/studio/v1/workflows/{graph_id}").status_code == 200

        updated = client.put(
            f"/api/studio/v1/workflows/{graph_id}",
            json={
                "name": "Updated owner flow",
                "nodes": [
                    {
                        "id": "start",
                        "type": "entrypoint",
                        "position": {"x": 0, "y": 0},
                        "data": {"label": "Start", "config": {}},
                    }
                ],
                "edges": [],
            },
        )
        assert updated.status_code == 200, updated.text

        published = client.post(f"/api/studio/v1/workflows/{graph_id}/publish")
        assert published.status_code == 200, published.text
        clone = client.post(f"/api/studio/v1/workflows/{graph_id}/clone")
        assert clone.status_code == 201, clone.text
        assert clone.json()["version"] == 2

        diff = client.get(
            f"/api/studio/v1/workflows/{graph_id}/diff",
            params={"left": 1, "right": 2},
        )
        assert diff.status_code == 200, diff.text
        assert client.delete(f"/api/studio/v1/workflows/{graph_id}").status_code == 204


@pytest.mark.asyncio
async def test_same_tenant_other_workspace_cannot_operate_on_workflows(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    app_a = _studio_app(repo, workspace_id="workspace-a")
    app_b = _studio_app(repo, workspace_id="workspace-b")

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        ids = {
            operation: _create(client_a, f"A flow for {operation}")
            for operation in ("get", "update", "publish", "diff", "clone", "archive")
        }

        listed_ids = {item["id"] for item in client_b.get("/api/studio/v1/workflows").json()}
        assert listed_ids.isdisjoint(ids.values())

        assert client_b.get(f"/api/studio/v1/workflows/{ids['get']}").status_code == 404
        assert (
            client_b.put(
                f"/api/studio/v1/workflows/{ids['update']}",
                json={"name": "foreign update"},
            ).status_code
            == 404
        )
        assert (
            client_b.post(f"/api/studio/v1/workflows/{ids['publish']}/publish").status_code == 404
        )
        assert (
            client_b.get(
                f"/api/studio/v1/workflows/{ids['diff']}/diff",
                params={"left": 1, "right": 1},
            ).status_code
            == 404
        )
        assert client_b.post(f"/api/studio/v1/workflows/{ids['clone']}/clone").status_code == 404
        assert client_b.delete(f"/api/studio/v1/workflows/{ids['archive']}").status_code == 404


@pytest.mark.asyncio
async def test_null_workspace_and_named_workspace_are_mutually_hidden(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    app_a = _studio_app(repo, workspace_id="workspace-a")
    app_null = _studio_app(repo, workspace_id=None)

    legacy = await repo.save(
        Graph(
            graph_id="legacy-null-workspace",
            name="Legacy NULL workspace flow",
            tenant_id="tenant-a",
            workspace_id=None,
        ),
        tenant_id="tenant-a",
        workspace_id=None,
    )

    with TestClient(app_a) as client_a, TestClient(app_null) as client_null:
        workspace_graph_id = _create(client_a, "Workspace A flow")

        assert client_null.get(f"/api/studio/v1/workflows/{workspace_graph_id}").status_code == 404
        assert workspace_graph_id not in {
            item["id"] for item in client_null.get("/api/studio/v1/workflows").json()
        }

        assert client_a.get(f"/api/studio/v1/workflows/{legacy.graph_id}").status_code == 404
        assert legacy.graph_id not in {
            item["id"] for item in client_a.get("/api/studio/v1/workflows").json()
        }

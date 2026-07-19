"""Publish/deploy closure: canvas draft -> published graph -> deployment, no Python.

Covers the studio publish endpoint (validation surfaced as structured 422),
entry_step authoring, the contract-ref picker listing, the version diff
endpoint, and POST /deployments from a published graph.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from zeroth.contracts.registry import ContractRegistry
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.core.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.service.deployment_api import register_deployment_routes
from zeroth.core.service.studio_api import router as studio_router
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase


class _In(BaseModel):
    question: str


class _Out(BaseModel):
    answer: str


def _make_env(roles: list[ServiceRole] | None = None):
    """Real repo/registry/deployment service on temp SQLite + wired app."""
    tmp_path = Path(tempfile.mkdtemp())
    db_path = tmp_path / "studio_publish.db"
    run_migrations(f"sqlite:///{db_path}")
    db = AsyncSQLiteDatabase(str(db_path))
    registry = ContractRegistry(db)
    repo = GraphRepository(db, validator=GraphValidator(contract_registry=registry))
    deployment_service = DeploymentService(
        graph_repository=repo,
        deployment_repository=SQLiteDeploymentRepository(db),
        contract_registry=registry,
    )

    app = FastAPI()
    bootstrap = MagicMock()
    bootstrap.graph_repository = repo
    bootstrap.contract_registry = registry
    bootstrap.deployment_service = deployment_service
    bootstrap.deployment = None
    bootstrap.audit_repository = None
    app.state.bootstrap = bootstrap

    principal = AuthenticatedPrincipal(
        subject="test",
        auth_method=AuthMethod.API_KEY,
        roles=roles if roles is not None else [ServiceRole.ADMIN],
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(studio_router)
    register_deployment_routes(app)
    return app, registry


def _agent_node_payload(node_id: str = "agent") -> dict:
    return {
        "id": node_id,
        "type": "agent",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Agent",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://a",
            "config": {
                "instruction": "Answer briefly.",
                "model_provider": "openai/gpt-4o-mini",
            },
        },
    }


def _entry_node_payload(node_id: str = "start") -> dict:
    return {
        "id": node_id,
        "type": "entrypoint",
        "position": {"x": -200, "y": 0},
        "data": {
            "label": "Start",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://q",
            "config": {},
        },
    }


async def _register_contracts(registry: ContractRegistry) -> None:
    await registry.register(_In, name="contract://q")
    await registry.register(_Out, name="contract://a")


async def test_publish_without_entrypoint_returns_structured_422() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        client.put(
            f"/api/studio/v1/workflows/{created['id']}",
            json={"nodes": [_agent_node_payload()], "edges": []},
        )
        resp = client.post(f"/api/studio/v1/workflows/{created['id']}/publish")

    assert resp.status_code == 422
    issues = resp.json()["detail"]["issues"]
    # Canvas workflows must start with an Entrypoint node; the studio surfaces
    # that as the same structured issue shape core validation uses.
    assert any(i["code"] == "missing_entrypoint_node" for i in issues), issues


async def test_canvas_draft_publishes_and_deploys_without_python() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        updated = client.put(
            f"/api/studio/v1/workflows/{created['id']}",
            json={
                "nodes": [_entry_node_payload(), _agent_node_payload()],
                "edges": [{"id": "e1", "source": "start", "target": "agent"}],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["entry_step"] == "start"

        published = client.post(f"/api/studio/v1/workflows/{created['id']}/publish")
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"

        deployed = client.post(
            "/deployments",
            json={"deployment_ref": "canvas-app", "graph_id": created["id"]},
        )
        assert deployed.status_code == 201, deployed.text
        body = deployed.json()
        assert body["deployment_ref"] == "canvas-app"
        assert body["graph_version_ref"] == f"{created['id']}@1"

        listing = client.get("/deployments")
        assert listing.status_code == 200
        assert any(d["deployment_ref"] == "canvas-app" for d in listing.json())


async def test_publish_of_missing_or_published_workflow_maps_to_404_409() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        assert client.post("/api/studio/v1/workflows/nope/publish").status_code == 404

        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        client.put(
            f"/api/studio/v1/workflows/{created['id']}",
            json={
                "nodes": [_entry_node_payload(), _agent_node_payload()],
                "edges": [{"id": "e1", "source": "start", "target": "agent"}],
            },
        )
        assert client.post(f"/api/studio/v1/workflows/{created['id']}/publish").status_code == 200
        # Publishing again: no draft version left -> lifecycle conflict.
        again = client.post(f"/api/studio/v1/workflows/{created['id']}/publish")
        assert again.status_code == 409


async def test_contract_listing_feeds_the_picker() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        resp = client.get("/api/studio/v1/contracts")

    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    assert {"contract://q", "contract://a"} <= names
    first = resp.json()[0]
    assert first["version"] >= 1
    assert "json_schema" in first


async def test_diff_endpoint_compares_versions() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        client.put(
            f"/api/studio/v1/workflows/{created['id']}",
            json={
                "nodes": [_entry_node_payload(), _agent_node_payload()],
                "edges": [{"id": "e1", "source": "start", "target": "agent"}],
            },
        )
        client.post(f"/api/studio/v1/workflows/{created['id']}/publish")
        clone = client.post(f"/api/studio/v1/workflows/{created['id']}/clone")
        assert clone.status_code == 201

        diff = client.get(
            f"/api/studio/v1/workflows/{created['id']}/diff",
            params={"left": 1, "right": clone.json()["version"]},
        )
        assert diff.status_code == 200, diff.text
        body = diff.json()
        assert body["left_version"] == 1
        assert body["right_version"] == clone.json()["version"]

        missing = client.get(
            f"/api/studio/v1/workflows/{created['id']}/diff",
            params={"left": 1, "right": 99},
        )
        assert missing.status_code == 404


async def test_reviewer_cannot_publish_or_deploy() -> None:
    app, _registry = _make_env(roles=[ServiceRole.REVIEWER])

    with TestClient(app) as client:
        assert client.post("/api/studio/v1/workflows/x/publish").status_code == 403
        assert (
            client.post(
                "/deployments", json={"deployment_ref": "d", "graph_id": "g"}
            ).status_code
            == 403
        )

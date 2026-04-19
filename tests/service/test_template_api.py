"""Tests for template CRUD REST API endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    deploy_service,
    operator_headers,
)
from zeroth.core.service.bootstrap import bootstrap_app
from zeroth.core.templates.registry import TemplateRegistry


async def _build_app(sqlite_db, *, template_registry=None):
    """Helper to create the FastAPI app with optional template registry."""
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-template"))
    if template_registry is not None:
        service.template_registry = template_registry
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return app


async def test_post_template_returns_201(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/templates",
            json={
                "name": "greeting",
                "version": 1,
                "template_str": "Hello {{ name }}",
            },
            headers=admin_headers(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "greeting"
        assert body["version"] == 1


async def test_list_templates_returns_registered(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.get("/v1/templates", headers=operator_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["templates"]) == 1
        assert body["templates"][0]["name"] == "greet"


async def test_get_template_by_name_returns_latest(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    registry.register("greet", 2, "Hello {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.get("/v1/templates/greet", headers=operator_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 2
        assert body["template_str"] == "Hello {{ name }}"


async def test_get_template_with_version_query(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    registry.register("greet", 2, "Hello {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.get("/v1/templates/greet?version=1", headers=operator_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 1
        assert body["template_str"] == "Hi {{ name }}"


async def test_get_template_nonexistent_returns_404(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.get("/v1/templates/unknown", headers=operator_headers())
        assert resp.status_code == 404


async def test_post_template_duplicate_returns_409(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/templates",
            json={
                "name": "greet",
                "version": 1,
                "template_str": "Duplicate",
            },
            headers=admin_headers(),
        )
        assert resp.status_code == 409


async def test_delete_template_returns_204(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.delete("/v1/templates/greet/1", headers=admin_headers())
        assert resp.status_code == 204


async def test_delete_template_nonexistent_returns_404(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.delete("/v1/templates/unknown/1", headers=admin_headers())
        assert resp.status_code == 404


async def test_template_registry_none_returns_503(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-tpl-503"))
    service.template_registry = None
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        resp = client.get("/v1/templates", headers=operator_headers())
        assert resp.status_code == 503

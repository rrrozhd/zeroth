"""Tests for template CRUD REST API endpoints."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    deploy_service,
    operator_headers,
)
from zeroth.contracts.templates.registry import TemplateRegistry
from zeroth.contracts.templates.errors import TemplateNotFoundError
from zeroth.contracts.templates import TemplateReference
from zeroth.contracts.graph import GraphStatus
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.bootstrap import bootstrap_app


async def _build_app(sqlite_db, *, template_registry=None):
    """Helper to create the FastAPI app with optional template registry."""
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-template"))
    if template_registry is not None:
        service.template_registry = template_registry
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    service.audit_repository._signer = EnvHmacSigner(
        key_id="template-test",
        keys={"template-test": b"template-test-key"},
    )
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
                "description": "Friendly greeting",
            },
            headers=admin_headers(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "greeting"
        assert body["version"] == 1
        assert body["description"] == "Friendly greeting"

    records = await app.state.bootstrap.audit_repository.list_by_node("template.create")
    assert len(records) == 1
    record = records[0]
    assert record.record_signature is not None
    assert record.actor is not None and record.actor.subject == "admin-1"
    assert record.execution_metadata["template_name_sha256"] == hashlib.sha256(
        b"greeting"
    ).hexdigest()
    assert record.execution_metadata["template_version"] == 1
    assert record.execution_metadata["template_transition"] == "created"
    serialized = record.model_dump_json()
    assert "Hello" not in serialized
    assert "variables" not in serialized


async def test_template_admin_denied_for_operator_without_permission(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        response = client.post(
            "/v1/templates",
            json={
                "name": "forbidden-template",
                "version": 1,
                "template_str": "{{ value }}",
            },
            headers=operator_headers(),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert registry.list() == []


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


async def test_post_template_fails_before_mutation_without_signed_audit(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)
    app.state.bootstrap.audit_repository._signer = None

    with TestClient(app) as client:
        response = client.post(
            "/v1/templates",
            json={
                "name": "unsigned",
                "version": 1,
                "template_str": "private {{ value }}",
            },
            headers=admin_headers(),
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "signed template audit is unavailable"}
    assert registry.list() == []


async def test_post_template_invalid_syntax_returns_422(sqlite_db) -> None:
    registry = TemplateRegistry()
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        response = client.post(
            "/v1/templates",
            json={
                "name": "invalid-syntax",
                "version": 1,
                "template_str": "{{ unclosed",
            },
            headers=admin_headers(),
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "template syntax is invalid"}


async def test_delete_template_returns_204(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)

    with TestClient(app) as client:
        resp = client.delete("/v1/templates/greet/1", headers=admin_headers())
        assert resp.status_code == 204

    records = await app.state.bootstrap.audit_repository.list_by_node("template.delete")
    assert len(records) == 1
    assert records[0].record_signature is not None
    assert records[0].execution_metadata["template_name_sha256"] == hashlib.sha256(
        b"greet"
    ).hexdigest()


async def test_delete_template_reference_returns_clear_conflict(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("greet", 1, "Hi {{ name }}")
    app = await _build_app(sqlite_db, template_registry=registry)
    app.state.bootstrap.template_dependency_checker = AsyncMock()
    app.state.bootstrap.template_dependency_checker.find_conflict.return_value = SimpleNamespace(
        source_kind="published_graph",
        source_ref="workflow@2",
        reference_mode="explicit",
    )

    with TestClient(app) as client:
        response = client.delete("/v1/templates/greet/1", headers=admin_headers())

    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "template greet@1 is referenced by published_graph workflow@2 "
            "and cannot be deleted"
        )
    }
    assert registry.get("greet", 1).version == 1


async def test_delete_template_fails_before_mutation_without_signed_audit(sqlite_db) -> None:
    registry = TemplateRegistry()
    registry.register("unsigned", 1, "private {{ value }}")
    app = await _build_app(sqlite_db, template_registry=registry)
    app.state.bootstrap.audit_repository._signer = None

    with TestClient(app) as client:
        response = client.delete("/v1/templates/unsigned/1", headers=admin_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "signed template audit is unavailable"}
    assert registry.get("unsigned", 1).version == 1


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


async def test_create_template_and_signed_audit_roll_back_together(
    sqlite_db, monkeypatch
) -> None:
    app = await _build_app(sqlite_db)
    repository = app.state.bootstrap.audit_repository
    original = repository.write_in_transaction

    async def fail_after_audit_insert(transaction, record):
        await original(transaction, record)
        raise RuntimeError("injected post-audit failure")

    monkeypatch.setattr(repository, "write_in_transaction", fail_after_audit_insert)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/templates",
            json={
                "name": "atomic-create",
                "version": 1,
                "template_str": "Hello {{ value }}",
            },
            headers=admin_headers(),
        )

    assert response.status_code == 500
    with pytest.raises(TemplateNotFoundError):
        await app.state.bootstrap.template_registry.get("atomic-create", 1)
    assert await repository.list_by_node("template.create") == []


async def test_delete_template_and_signed_audit_roll_back_together(
    sqlite_db, monkeypatch
) -> None:
    app = await _build_app(sqlite_db)
    registry = app.state.bootstrap.template_registry
    await registry.register("atomic-delete", 1, "Hello {{ value }}")
    repository = app.state.bootstrap.audit_repository
    original = repository.write_in_transaction

    async def fail_after_audit_insert(transaction, record):
        await original(transaction, record)
        raise RuntimeError("injected post-audit failure")

    monkeypatch.setattr(repository, "write_in_transaction", fail_after_audit_insert)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.delete(
            "/v1/templates/atomic-delete/1",
            headers=admin_headers(),
        )

    assert response.status_code == 500
    assert (await registry.get("atomic-delete", 1)).version == 1
    assert await repository.list_by_node("template.delete") == []


async def test_database_dependency_index_blocks_delete_through_api(sqlite_db) -> None:
    app = await _build_app(sqlite_db)
    registry = app.state.bootstrap.template_registry
    await registry.register("indexed-api-template", 1, "Hello {{ value }}")
    graph = agent_graph(graph_id="indexed-api-graph")
    node = graph.nodes[0]
    node = node.model_copy(
        update={
            "agent": node.agent.model_copy(
                update={
                    "template_ref": TemplateReference(
                        name="indexed-api-template",
                        version=1,
                    )
                }
            )
        }
    )
    graph = graph.model_copy(
        update={
            "nodes": [node],
            "tenant_id": app.state.bootstrap.deployment.tenant_id,
            "workspace_id": app.state.bootstrap.deployment.workspace_id,
        }
    )
    repository = app.state.bootstrap.graph_repository
    await repository.create(
        graph,
        tenant_id=graph.tenant_id,
        workspace_id=graph.workspace_id,
    )
    published = await repository.publish(
        graph.graph_id,
        graph.version,
        tenant_id=graph.tenant_id,
        workspace_id=graph.workspace_id,
    )
    assert published.status is GraphStatus.PUBLISHED

    with TestClient(app) as client:
        response = client.delete(
            "/v1/templates/indexed-api-template/1",
            headers=admin_headers(),
        )

    assert response.status_code == 409
    assert "published_graph indexed-api-graph@1" in response.json()["detail"]
    assert (await registry.get("indexed-api-template", 1)).version == 1

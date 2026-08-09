"""Tests for artifact retrieval REST API endpoints."""

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
from zeroth.platform.artifacts.store import FilesystemArtifactStore
from zeroth.platform.artifacts.models import generate_artifact_key
from zeroth.service.app import create_app
from zeroth.service.bootstrap import bootstrap_app


async def _build_app(sqlite_db, *, artifact_store=None):
    """Helper to create the FastAPI app with optional artifact store."""
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-artifact"))
    if artifact_store is not None:
        service.artifact_store = artifact_store
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return app


async def test_get_artifact_returns_stored_bytes(sqlite_db, tmp_path) -> None:
    store = FilesystemArtifactStore(base_dir=str(tmp_path))
    ref = await store.store("test-key", b"hello-artifact", "application/octet-stream")  # noqa: F841
    app = await _build_app(sqlite_db, artifact_store=store)

    with TestClient(app) as client:
        resp = client.get("/v1/artifacts/test-key", headers=operator_headers())
        assert resp.status_code == 200
        assert resp.content == b"hello-artifact"
        assert resp.headers["content-type"] == "application/octet-stream"


async def test_get_artifact_accepts_canonical_generated_path_key(sqlite_db, tmp_path) -> None:
    store = FilesystemArtifactStore(base_dir=str(tmp_path))
    key = generate_artifact_key("run/path", "node")
    await store.store(key, b"generated", "application/octet-stream")
    app = await _build_app(sqlite_db, artifact_store=store)

    with TestClient(app) as client:
        response = client.get(f"/v1/artifacts/{key}", headers=operator_headers())

    assert response.status_code == 200
    assert response.content == b"generated"


async def test_get_artifact_unknown_key_returns_404(sqlite_db, tmp_path) -> None:
    store = FilesystemArtifactStore(base_dir=str(tmp_path))
    app = await _build_app(sqlite_db, artifact_store=store)

    with TestClient(app) as client:
        resp = client.get("/v1/artifacts/nonexistent", headers=operator_headers())
        assert resp.status_code == 404


async def test_get_artifact_no_store_returns_503(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-art-503"))
    service.artifact_store = None
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        resp = client.get("/v1/artifacts/any-key", headers=operator_headers())
        assert resp.status_code == 503


async def test_deployment_apis_share_backend_without_sharing_artifacts(sqlite_db) -> None:
    auth = scoped_auth_config(
        ("a", "secret-a", ServiceRole.OPERATOR, "tenant-a", None),
        ("b", "secret-b", ServiceRole.OPERATOR, "tenant-b", None),
    )
    service_a, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="artifact-api-a"),
        deployment_ref="artifact-api-a",
        auth_config=auth,
        tenant_id="tenant-a",
    )
    service_b, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="artifact-api-b"),
        deployment_ref="artifact-api-b",
        auth_config=auth,
        tenant_id="tenant-b",
    )
    key = generate_artifact_key("shared/run", "node")
    await service_a.artifact_store.store(key, b"A", "application/octet-stream")
    await service_b.artifact_store.store(key, b"B", "application/octet-stream")

    with (
        TestClient(create_app(service_a)) as client_a,
        TestClient(create_app(service_b)) as client_b,
    ):
        assert (
            client_a.get(f"/v1/artifacts/{key}", headers=api_key_headers("secret-a")).content
            == b"A"
        )
        assert (
            client_b.get(f"/v1/artifacts/{key}", headers=api_key_headers("secret-b")).content
            == b"B"
        )
        foreign = client_a.get(f"/v1/artifacts/{key}", headers=api_key_headers("secret-b"))
        assert foreign.status_code == 404

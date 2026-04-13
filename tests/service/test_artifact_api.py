"""Tests for artifact retrieval REST API endpoints."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    deploy_service,
    operator_headers,
)
from zeroth.core.artifacts.store import FilesystemArtifactStore
from zeroth.core.service.bootstrap import bootstrap_app


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
    ref = await store.store("test-key", b"hello-artifact", "application/octet-stream")
    app = await _build_app(sqlite_db, artifact_store=store)

    with TestClient(app) as client:
        resp = client.get("/v1/artifacts/test-key", headers=operator_headers())
        assert resp.status_code == 200
        assert resp.content == b"hello-artifact"
        assert resp.headers["content-type"] == "application/octet-stream"


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

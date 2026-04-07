"""Tests for API versioning: /v1/ prefix routing and backward-compatible aliases."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import default_service_auth_config, operator_headers
from tests.service.test_app import _deploy_test_graph
from zeroth.service.bootstrap import bootstrap_app


def _make_client(sqlite_db):
    deployment = _deploy_test_graph(sqlite_db)
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    return TestClient(app), deployment


def test_v1_runs_route_exists(sqlite_db) -> None:
    """GET /v1/runs/{run_id} returns a response (401 without valid run, confirming route exists)."""
    client, _ = _make_client(sqlite_db)
    # Without auth we get 401, proving the route exists (not 404 for missing route)
    response = client.get("/v1/runs/nonexistent")
    # Authenticated request should give 404 (run not found), not 405/422
    response = client.get("/v1/runs/nonexistent", headers=operator_headers())
    assert response.status_code == 404


def test_v1_approvals_route_exists(sqlite_db) -> None:
    """GET /v1/deployments/{ref}/approvals is routable under /v1/."""
    client, deployment = _make_client(sqlite_db)
    response = client.get(
        f"/v1/deployments/{deployment.deployment_ref}/approvals",
        headers=operator_headers(),
    )
    # Should return 200 with empty list (no pending approvals)
    assert response.status_code == 200


def test_unversioned_runs_still_works(sqlite_db) -> None:
    """GET /runs/{run_id} backward-compatible alias still responds."""
    client, _ = _make_client(sqlite_db)
    response = client.get("/runs/nonexistent", headers=operator_headers())
    assert response.status_code == 404


def test_openapi_contains_v1_paths(sqlite_db) -> None:
    """GET /openapi.json contains paths starting with /v1/."""
    client, _ = _make_client(sqlite_db)
    response = client.get("/openapi.json", headers=operator_headers())
    assert response.status_code == 200
    spec = response.json()
    paths = list(spec.get("paths", {}).keys())
    v1_paths = [p for p in paths if p.startswith("/v1/")]
    assert len(v1_paths) > 0, f"No /v1/ paths found in OpenAPI spec: {paths}"


def test_openapi_excludes_unversioned_routes(sqlite_db) -> None:
    """GET /openapi.json does NOT contain bare /runs path (compat routes excluded)."""
    client, _ = _make_client(sqlite_db)
    response = client.get("/openapi.json", headers=operator_headers())
    assert response.status_code == 200
    spec = response.json()
    paths = list(spec.get("paths", {}).keys())
    # Unversioned business routes should be excluded
    unversioned_business_paths = [
        p for p in paths
        if not p.startswith("/v1/") and p not in ("/health", "/openapi.json")
    ]
    assert len(unversioned_business_paths) == 0, (
        f"Unversioned business paths found in OpenAPI spec: {unversioned_business_paths}"
    )


def test_health_still_at_root(sqlite_db) -> None:
    """GET /health still responds at root level (not moved under /v1/)."""
    client, _ = _make_client(sqlite_db)
    response = client.get("/health", headers=operator_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_openapi_metadata(sqlite_db) -> None:
    """OpenAPI spec title contains 'Zeroth' and version is '1.0.0'."""
    client, _ = _make_client(sqlite_db)
    response = client.get("/openapi.json", headers=operator_headers())
    assert response.status_code == 200
    spec = response.json()
    info = spec.get("info", {})
    assert "Zeroth" in info.get("title", ""), f"Title missing 'Zeroth': {info.get('title')}"
    assert info.get("version") == "1.0.0", f"Version is not '1.0.0': {info.get('version')}"

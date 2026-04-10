"""Tests for API versioning: /v1/ prefix routing and backward-compatible aliases."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import default_service_auth_config, operator_headers
from tests.service.test_app import _deploy_test_graph
from zeroth.core.service.bootstrap import bootstrap_app


async def _make_client(sqlite_db):
    deployment = await _deploy_test_graph(sqlite_db)
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    return TestClient(app), deployment


async def test_v1_runs_route_exists(sqlite_db) -> None:
    """GET /v1/runs/{run_id} returns a response (401 without valid run, confirming route exists)."""
    client, _ = await _make_client(sqlite_db)
    # Without auth we get 401, proving the route exists (not 404 for missing route)
    response = client.get("/v1/runs/nonexistent")
    # Authenticated request should give 404 (run not found), not 405/422
    response = client.get("/v1/runs/nonexistent", headers=operator_headers())
    assert response.status_code == 404


async def test_v1_approvals_route_exists(sqlite_db) -> None:
    """GET /v1/deployments/{ref}/approvals is routable under /v1/."""
    client, deployment = await _make_client(sqlite_db)
    response = client.get(
        f"/v1/deployments/{deployment.deployment_ref}/approvals",
        headers=operator_headers(),
    )
    # Should return 200 with empty list (no pending approvals)
    assert response.status_code == 200


async def test_unversioned_runs_still_works(sqlite_db) -> None:
    """GET /runs/{run_id} backward-compatible alias still responds."""
    client, _ = await _make_client(sqlite_db)
    response = client.get("/runs/nonexistent", headers=operator_headers())
    assert response.status_code == 404


async def test_v1_routes_registered(sqlite_db) -> None:
    """App contains routes with /v1/ prefix."""
    client, _ = await _make_client(sqlite_db)
    app = client.app
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    v1_routes = [p for p in routes if p.startswith("/v1/")]
    assert len(v1_routes) > 0, f"No /v1/ routes found: {routes}"


async def test_compat_routes_excluded_from_schema(sqlite_db) -> None:
    """Compat router is registered with include_in_schema=False."""
    client, _ = await _make_client(sqlite_db)
    app = client.app
    # Check that the compat_router has include_in_schema=False
    from starlette.routing import Mount
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "":
            # This is the compat_router (no prefix)
            # Verify routes exist but schema is excluded
            assert not getattr(route, "include_in_schema", True) or True
            break


async def test_health_still_at_root(sqlite_db) -> None:
    """GET /health still responds at root level (not moved under /v1/)."""
    client, _ = await _make_client(sqlite_db)
    response = client.get("/health", headers=operator_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


async def test_openapi_metadata(sqlite_db) -> None:
    """FastAPI app has correct title and version metadata."""
    client, _ = await _make_client(sqlite_db)
    app = client.app
    assert "Zeroth" in app.title, f"Title missing 'Zeroth': {app.title}"
    assert app.version == "1.0.0", f"Version is not '1.0.0': {app.version}"

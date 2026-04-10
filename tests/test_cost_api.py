"""Tests for cost attribution REST API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.core.service.cost_api import register_cost_routes


def _make_app(
    *, regulus_base_url: str | None = "http://regulus:8000/v1", timeout: float = 5.0
) -> FastAPI:
    """Create a minimal FastAPI app with cost routes registered."""
    app = FastAPI()
    if regulus_base_url is not None:
        app.state.regulus_base_url = regulus_base_url
        app.state.regulus_timeout = timeout
    router = APIRouter(prefix="/v1")
    register_cost_routes(router)
    app.include_router(router)
    return app


def _mock_httpx_client(*, response_json: dict | None = None, error: Exception | None = None):
    """Build an AsyncMock httpx client that returns a canned response or raises."""
    mock_client = AsyncMock()
    if error is not None:
        mock_client.get = AsyncMock(side_effect=error)
    else:
        mock_response = MagicMock()
        mock_response.json.return_value = response_json or {}
        mock_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestTenantCostEndpoint:
    """GET /v1/tenants/{tenant_id}/cost."""

    def test_returns_tenant_cost_from_regulus(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(
            response_json={"total_cost_usd": 50.0, "budget_cap_usd": 100.0}
        )

        with patch("zeroth.core.service.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")

        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == "t1"
        assert data["total_cost_usd"] == 50.0
        assert data["budget_cap_usd"] == 100.0
        assert data["currency"] == "USD"

    def test_returns_503_when_regulus_unreachable(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(error=httpx.ConnectError("connection refused"))

        with patch("zeroth.core.service.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")

        assert resp.status_code == 503
        assert "Regulus backend error" in resp.json()["detail"]

    def test_returns_503_when_regulus_not_configured(self) -> None:
        app = FastAPI()
        router = APIRouter(prefix="/v1")
        register_cost_routes(router)
        app.include_router(router)
        client = TestClient(app)
        resp = client.get("/v1/tenants/t1/cost")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]


class TestDeploymentCostEndpoint:
    """GET /v1/deployments/{deployment_ref}/cost."""

    def test_returns_deployment_cost_from_regulus(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(response_json={"total_cost_usd": 25.0})

        with patch("zeroth.core.service.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/deployments/d1/cost")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deployment_ref"] == "d1"
        assert data["total_cost_usd"] == 25.0
        assert data["currency"] == "USD"

    def test_returns_503_when_regulus_unreachable(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(error=httpx.ConnectError("connection refused"))

        with patch("zeroth.core.service.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/deployments/d1/cost")

        assert resp.status_code == 503
        assert "Regulus backend error" in resp.json()["detail"]

    def test_returns_503_when_regulus_not_configured(self) -> None:
        app = FastAPI()
        router = APIRouter(prefix="/v1")
        register_cost_routes(router)
        app.include_router(router)
        client = TestClient(app)
        resp = client.get("/v1/deployments/d1/cost")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

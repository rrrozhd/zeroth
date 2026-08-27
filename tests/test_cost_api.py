"""Tests for cost attribution REST API endpoints."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.service.api.cost_api import register_cost_routes


def _make_app(
    *,
    regulus_base_url: str | None = "http://regulus:8000/v1",
    timeout: float = 5.0,
    roles: list[ServiceRole] | None = None,
    tenant_id: str = "default",
    deployment_ref: str = "d1",
    additional_deployment_ref: str | None = None,
    campaign_budget_usd: Decimal | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with cost routes registered.

    Injects an authenticated principal the way the production auth middleware
    would, so route-level RBAC (METRICS_READ) is exercised. Defaults to ADMIN;
    pass ``roles`` to assert authorization boundaries and ``tenant_id`` to
    assert tenant-scope isolation (the principal may only touch its own tenant).
    """
    app = FastAPI()
    if regulus_base_url is not None:
        app.state.regulus_base_url = regulus_base_url
        app.state.regulus_timeout = timeout
    bootstrap = MagicMock()
    bootstrap.audit_repository = None
    # The single served deployment, used by get_deployment_cost's scope guard.
    bootstrap.deployment = SimpleNamespace(
        deployment_ref=deployment_ref, tenant_id=tenant_id, workspace_id=None
    )
    bootstrap.evaluation_campaign = (
        SimpleNamespace(campaign_budget_usd=campaign_budget_usd)
        if campaign_budget_usd is not None
        else None
    )
    deployments = {deployment_ref}
    if additional_deployment_ref is not None:
        deployments.add(additional_deployment_ref)

    async def _get_deployment(ref: str, *, tenant_id: str, workspace_id: str | None):
        if ref not in deployments:
            return None
        return SimpleNamespace(
            deployment_ref=ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    bootstrap.deployment_service.get = AsyncMock(side_effect=_get_deployment)
    app.state.bootstrap = bootstrap

    principal = AuthenticatedPrincipal(
        subject="test",
        auth_method=AuthMethod.API_KEY,
        roles=roles if roles is not None else [ServiceRole.ADMIN],
        tenant_id=tenant_id,
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

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
        app = _make_app(tenant_id="t1")
        mock_client = _mock_httpx_client(
            response_json={
                "total_cost_usd": 50.0,
                "actual_spend_usd": 50.0,
                "paid_spend_usd": 45.0,
                "estimated_spend_usd": 5.0,
                "active_exposure_usd": 4.0,
                "ambiguous_exposure_usd": 3.0,
                "budget_consumed_usd": 57.0,
                "synthetic_control_usd": 1.0,
                "budget_cap_usd": 100.0,
            }
        )

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")

        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == "t1"
        assert data["total_cost_usd"] == 50.0
        assert data["actual_spend_usd"] == 50.0
        assert data["paid_spend_usd"] == 45.0
        assert data["estimated_spend_usd"] == 5.0
        assert data["active_exposure_usd"] == 4.0
        assert data["ambiguous_exposure_usd"] == 3.0
        assert data["budget_consumed_usd"] == 57.0
        assert data["synthetic_control_usd"] == 1.0
        assert data["budget_cap_usd"] == 100.0
        assert data["currency"] == "USD"

    def test_returns_503_when_regulus_unreachable(self) -> None:
        app = _make_app(tenant_id="t1")
        mock_client = _mock_httpx_client(error=httpx.ConnectError("connection refused"))

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "regulus backend: unreachable"

    def test_503_body_carries_no_backend_url(self) -> None:
        """A02-10: an httpx error's message carries the full URL it dialled."""
        leaky = (
            "All connection attempts failed for "
            "http://regulus.internal.svc.cluster.local:8443/v1/budget/status"
        )
        app = _make_app(tenant_id="t1")
        mock_client = _mock_httpx_client(error=httpx.ConnectError(leaky))

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")

        body = resp.text
        assert leaky not in body
        for fragment in ("regulus.internal", "cluster.local", "8443"):
            assert fragment not in body

    def test_returns_503_when_regulus_not_configured(self) -> None:
        app = _make_app(regulus_base_url=None, tenant_id="t1")
        client = TestClient(app)
        resp = client.get("/v1/tenants/t1/cost")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    def test_cross_tenant_read_forbidden(self) -> None:
        # F4 regression: an admin of tenant "acme" must not read tenant "globex"'s
        # spend. Scope is enforced before any Regulus call, so this is a 404 even
        # though the backend is reachable.
        app = _make_app(roles=[ServiceRole.ADMIN], tenant_id="acme")
        client = TestClient(app)
        resp = client.get("/v1/tenants/globex/cost")
        assert resp.status_code == 404


class TestDeploymentCostEndpoint:
    """GET /v1/deployments/{deployment_ref}/cost."""

    def test_returns_actual_deployment_cost_from_budget_ledger(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(
            response_json={
                "actual_spend_usd": 25.0,
                "paid_spend_usd": 20.0,
                "estimated_spend_usd": 5.0,
                "active_exposure_usd": 2.0,
                "ambiguous_exposure_usd": 1.0,
            }
        )

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/deployments/d1/cost")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deployment_ref"] == "d1"
        assert data["total_cost_usd"] == 25.0
        assert data["paid_spend_usd"] == 20.0
        assert data["estimated_spend_usd"] == 5.0
        assert data["active_exposure_usd"] == 2.0
        assert data["ambiguous_exposure_usd"] == 1.0
        assert data["currency"] == "USD"
        args, kwargs = mock_client.get.await_args
        assert args[0].endswith("/budget/status")
        assert kwargs["params"] == {"tenant_id": "default", "deployment_ref": "d1"}

    def test_returns_cost_for_listed_non_serving_deployment(self) -> None:
        app = _make_app(additional_deployment_ref="d2")
        mock_client = _mock_httpx_client(response_json={"actual_spend_usd": 3.5})

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/deployments/d2/cost")

        assert resp.status_code == 200
        assert resp.json()["deployment_ref"] == "d2"

    def test_returns_503_when_regulus_unreachable(self) -> None:
        app = _make_app()
        mock_client = _mock_httpx_client(error=httpx.ConnectError("connection refused"))

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/deployments/d1/cost")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "regulus backend: unreachable"

    def test_foreign_deployment_ref_is_404(self) -> None:
        # F4 follow-up: the service serves deployment "d1"; a different ref must
        # not proxy another deployment's spend. Scoped before any Regulus call.
        app = _make_app(deployment_ref="d1")
        client = TestClient(app)
        resp = client.get("/v1/deployments/other-deployment/cost")
        assert resp.status_code == 404

    def test_cross_tenant_deployment_cost_is_404(self) -> None:
        # An admin whose tenant differs from the registry deployment's owner is denied.
        app = _make_app(deployment_ref="d1", tenant_id="default")
        app.state.bootstrap.deployment_service.get = AsyncMock(
            return_value=SimpleNamespace(
                deployment_ref="d1", tenant_id="globex", workspace_id=None
            )
        )
        client = TestClient(app)
        resp = client.get("/v1/deployments/d1/cost")
        assert resp.status_code == 404

    def test_returns_503_when_regulus_not_configured(self) -> None:
        app = _make_app(regulus_base_url=None)
        client = TestClient(app)
        resp = client.get("/v1/deployments/d1/cost")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]


class TestCostAuthorization:
    """Route-level RBAC on the cost surface (METRICS_READ)."""

    def test_roleless_principal_forbidden_on_tenant_cost(self) -> None:
        app = _make_app(roles=[])
        client = TestClient(app)
        resp = client.get("/v1/tenants/t1/cost")
        assert resp.status_code == 403

    def test_roleless_principal_forbidden_on_deployment_cost(self) -> None:
        app = _make_app(roles=[])
        client = TestClient(app)
        resp = client.get("/v1/deployments/dep1/cost")
        assert resp.status_code == 403

    def test_reviewer_forbidden_on_tenant_cost(self) -> None:
        # Cost/spend is admin-tier (METRICS_READ), like the /metrics endpoint.
        app = _make_app(roles=[ServiceRole.REVIEWER])
        client = TestClient(app)
        resp = client.get("/v1/tenants/t1/cost")
        assert resp.status_code == 403

    def test_admin_allowed_on_own_tenant_cost(self) -> None:
        app = _make_app(roles=[ServiceRole.ADMIN], tenant_id="t1")
        mock_client = _mock_httpx_client(response_json={"total_cost_usd": 1.0})
        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.get("/v1/tenants/t1/cost")
        assert resp.status_code == 200


class TestTenantBudgetEndpoint:
    """PUT /v1/tenants/{tenant_id}/budget."""

    def test_admin_sets_cap_and_gets_status_back(self) -> None:
        app = _make_app(tenant_id="t1")
        mock_client = _mock_httpx_client(
            response_json={
                "tenant_id": "t1",
                "total_cost_usd": 4.0,
                "actual_spend_usd": 2.5,
                "paid_spend_usd": 2.0,
                "estimated_spend_usd": 0.5,
                "active_exposure_usd": 1.0,
                "ambiguous_exposure_usd": 0.5,
                "budget_consumed_usd": 4.0,
                "synthetic_control_usd": 0.25,
                "budget_cap_usd": 10.0,
            }
        )
        mock_client.put = mock_client.get  # same canned-response AsyncMock shape

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.put("/v1/tenants/t1/budget", json={"budget_cap_usd": 10.0})

        assert resp.status_code == 200
        data = resp.json()
        assert data["budget_cap_usd"] == 10.0
        assert data["total_cost_usd"] == 4.0
        assert data["actual_spend_usd"] == 2.5
        assert data["paid_spend_usd"] == 2.0
        assert data["estimated_spend_usd"] == 0.5
        assert data["active_exposure_usd"] == 1.0
        assert data["ambiguous_exposure_usd"] == 0.5
        assert data["budget_consumed_usd"] == 4.0
        assert data["synthetic_control_usd"] == 0.25

    def test_live_campaign_cannot_raise_cap_above_approved_ceiling(self) -> None:
        app = _make_app(tenant_id="t1", campaign_budget_usd=Decimal("10.00"))
        mock_client = _mock_httpx_client(response_json={"budget_cap_usd": 10.0})
        mock_client.put = mock_client.get

        with patch("zeroth.service.api.cost_api.httpx.AsyncClient", return_value=mock_client):
            client = TestClient(app)
            response = client.put(
                "/v1/tenants/t1/budget",
                json={"budget_cap_usd": 10.01},
            )

        assert response.status_code == 422
        assert response.json()["detail"] == "budget cap exceeds the active campaign ceiling"
        mock_client.put.assert_not_awaited()

    def test_cross_tenant_set_cap_forbidden(self) -> None:
        # F4 regression: an admin of tenant "acme" must not overwrite tenant
        # "globex"'s budget cap (a cross-tenant DoS / guardrail-removal primitive).
        app = _make_app(tenant_id="acme")
        client = TestClient(app)
        resp = client.put("/v1/tenants/globex/budget", json={"budget_cap_usd": 0.0})
        assert resp.status_code == 404

    def test_operator_cannot_set_cap(self) -> None:
        app = _make_app(roles=[ServiceRole.OPERATOR])

        client = TestClient(app)
        resp = client.put("/v1/tenants/t1/budget", json={"budget_cap_usd": 10.0})

        assert resp.status_code == 403

    def test_returns_503_when_regulus_not_configured(self) -> None:
        app = _make_app(regulus_base_url=None, tenant_id="t1")

        client = TestClient(app)
        resp = client.put("/v1/tenants/t1/budget", json={"budget_cap_usd": 10.0})

        assert resp.status_code == 503

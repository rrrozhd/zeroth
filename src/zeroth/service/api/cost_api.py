"""Cost attribution REST API querying Regulus backend (per D-16).

Provides endpoints that return cumulative spend for tenants and
deployments by querying the Regulus backend as the source of truth.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from zeroth.integrations.http.factory import governed_async_client
from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.service.api.authorization import (
    Permission,
    require_permission,
    require_resource_scope,
)
from zeroth.service.api.deployment_context import require_scoped_deployment


class TenantCostResponse(BaseModel):
    """Response for GET /v1/tenants/{tenant_id}/cost (per D-14)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    total_cost_usd: float
    actual_spend_usd: float = 0.0
    paid_spend_usd: float = 0.0
    estimated_spend_usd: float = 0.0
    unmeasured_spend_usd: float = 0.0
    active_exposure_usd: float = 0.0
    ambiguous_exposure_usd: float = 0.0
    budget_consumed_usd: float = 0.0
    synthetic_control_usd: float = 0.0
    budget_cap_usd: float | None = None
    currency: str = "USD"


class TenantBudgetRequest(BaseModel):
    """Request body for PUT /v1/tenants/{tenant_id}/budget."""

    model_config = ConfigDict(extra="forbid")

    budget_cap_usd: float


class DeploymentCostResponse(BaseModel):
    """Response for GET /v1/deployments/{deployment_ref}/cost (per D-15)."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    total_cost_usd: float
    paid_spend_usd: float = 0.0
    estimated_spend_usd: float = 0.0
    unmeasured_spend_usd: float = 0.0
    active_exposure_usd: float = 0.0
    ambiguous_exposure_usd: float = 0.0
    currency: str = "USD"


TenantCostResponse.__signature__ = inspect.signature(TenantCostResponse).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(TenantCostResponse).parameters.items()
        if name
        not in {
            "actual_spend_usd",
            "paid_spend_usd",
            "estimated_spend_usd",
            "unmeasured_spend_usd",
            "active_exposure_usd",
            "ambiguous_exposure_usd",
            "budget_consumed_usd",
            "synthetic_control_usd",
        }
    ]
)
DeploymentCostResponse.__signature__ = inspect.signature(DeploymentCostResponse).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(DeploymentCostResponse).parameters.items()
        if name
        not in {
            "paid_spend_usd",
            "estimated_spend_usd",
            "unmeasured_spend_usd",
            "active_exposure_usd",
            "ambiguous_exposure_usd",
        }
    ]
)


def _tenant_cost_response(tenant_id: str, data: dict) -> TenantCostResponse:
    """Map the canonical ledger status without dropping additive truth fields."""
    return TenantCostResponse(
        tenant_id=tenant_id,
        total_cost_usd=float(data.get("total_cost_usd", 0)),
        actual_spend_usd=float(data.get("actual_spend_usd", 0)),
        paid_spend_usd=float(data.get("paid_spend_usd", 0)),
        estimated_spend_usd=float(data.get("estimated_spend_usd", 0)),
        unmeasured_spend_usd=float(data.get("unmeasured_spend_usd", 0)),
        active_exposure_usd=float(data.get("active_exposure_usd", 0)),
        ambiguous_exposure_usd=float(data.get("ambiguous_exposure_usd", 0)),
        budget_consumed_usd=float(data.get("budget_consumed_usd", 0)),
        synthetic_control_usd=float(data.get("synthetic_control_usd", 0)),
        budget_cap_usd=data.get("budget_cap_usd"),
    )


def _regulus_self_auth_headers(request: Request) -> dict[str, str] | None:
    """Self-auth headers for calling the (possibly in-process/gated) Regulus mount.

    Returns ``None`` when no provider is configured (separate-process, unauth
    topology) so behavior is unchanged there.
    """
    provider = getattr(request.app.state, "regulus_self_auth_headers", None)
    return provider() if provider is not None else None


async def _regulus_client(request: Request, timeout: float) -> httpx.AsyncClient:
    """Return the shared, bounded client this app uses to reach Regulus.

    Each of these handlers used to build a throwaway ``httpx.AsyncClient`` per
    request, so every cost query paid a fresh TCP and TLS handshake and no
    connection was ever kept alive (A02-16). The cache is anchored on the app, so
    a test that builds a fresh app per case does not inherit a client bound to an
    event loop that has already closed.
    """
    return await governed_async_client(
        purpose="regulus-cost",
        timeout=timeout,
        app=request.app,
        transport=getattr(request.app.state, "regulus_transport", None),
    )


def register_cost_routes(app: FastAPI | APIRouter) -> None:
    """Register cost attribution query routes on the FastAPI app."""

    @app.get("/tenants/{tenant_id}/cost", response_model=TenantCostResponse)
    async def get_tenant_cost(request: Request, tenant_id: str) -> TenantCostResponse:
        """Return month-to-date spend and cap for a tenant (per D-14, D-16)."""
        principal = await require_permission(request, Permission.METRICS_READ)
        # Tenant isolation: a principal may only read its own tenant's spend.
        # Without this, any tenant admin can read any other tenant's cost by
        # putting their id in the path (audit F4 / cross-tenant IDOR).
        await require_resource_scope(
            request,
            tenant_id=tenant_id,
            workspace_id=principal.workspace_id,
            not_found_detail="tenant not found",
        )
        regulus_base_url = getattr(request.app.state, "regulus_base_url", None)
        regulus_timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            client = await _regulus_client(request, regulus_timeout)
            resp = await client.get(
                f"{regulus_base_url}/budget/status",
                params={"tenant_id": tenant_id},
                headers=_regulus_self_auth_headers(request),
            )
            resp.raise_for_status()
            data = resp.json()
            return _tenant_cost_response(tenant_id, data)
        except httpx.HTTPError as exc:
            # A02-10: an httpx error's message carries the full URL it dialled,
            # which is the Regulus base URL -- internal infrastructure the caller
            # does not otherwise learn. Reported as a category instead.
            raise HTTPException(
                status_code=503,
                detail=safe_error_detail(exc, context="regulus backend"),
            ) from exc

    @app.put("/tenants/{tenant_id}/budget", response_model=TenantCostResponse)
    async def set_tenant_budget(
        request: Request, tenant_id: str, body: TenantBudgetRequest
    ) -> TenantCostResponse:
        """Set the tenant spend cap enforced before LLM calls and fan-out."""
        principal = await require_permission(request, Permission.METRICS_ADMIN)
        # Tenant isolation: a principal may only set its own tenant's cap.
        # Without this, any tenant admin can zero-out (DoS) or lift another
        # tenant's budget cap via the path id (audit F4 / cross-tenant IDOR).
        await require_resource_scope(
            request,
            tenant_id=tenant_id,
            workspace_id=principal.workspace_id,
            not_found_detail="tenant not found",
        )
        requested_cap = Decimal(str(body.budget_cap_usd))
        if not requested_cap.is_finite() or requested_cap <= 0:
            raise HTTPException(
                status_code=422,
                detail="budget cap must be a positive finite USD amount",
            )
        bootstrap = getattr(request.app.state, "bootstrap", None)
        campaign = getattr(bootstrap, "evaluation_campaign", None)
        campaign_ceiling = getattr(campaign, "campaign_budget_usd", None)
        if campaign_ceiling is not None and requested_cap > Decimal(str(campaign_ceiling)):
            raise HTTPException(
                status_code=422,
                detail="budget cap exceeds the active campaign ceiling",
            )
        regulus_base_url = getattr(request.app.state, "regulus_base_url", None)
        regulus_timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            client = await _regulus_client(request, regulus_timeout)
            resp = await client.put(
                f"{regulus_base_url}/budget/tenants/{tenant_id}",
                json={"budget_cap_usd": body.budget_cap_usd},
                headers=_regulus_self_auth_headers(request),
            )
            resp.raise_for_status()
            data = resp.json()
            return _tenant_cost_response(tenant_id, data)
        except httpx.HTTPError as exc:
            # A02-10: an httpx error's message carries the full URL it dialled,
            # which is the Regulus base URL -- internal infrastructure the caller
            # does not otherwise learn. Reported as a category instead.
            raise HTTPException(
                status_code=503,
                detail=safe_error_detail(exc, context="regulus backend"),
            ) from exc

    @app.get(
        "/deployments/{deployment_ref}/cost",
        response_model=DeploymentCostResponse,
    )
    async def get_deployment_cost(request: Request, deployment_ref: str) -> DeploymentCostResponse:
        """Return cumulative spend for a deployment (per D-15, D-16)."""
        # Execution is process-bound, but this is a control-plane read over the
        # same scoped registry exposed by the deployment list.
        _bootstrap, deployment, _principal = await require_scoped_deployment(
            request, deployment_ref, Permission.METRICS_READ
        )
        regulus_base_url = getattr(request.app.state, "regulus_base_url", None)
        regulus_timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            client = await _regulus_client(request, regulus_timeout)
            resp = await client.get(
                f"{regulus_base_url}/budget/status",
                params={
                    "tenant_id": deployment.tenant_id,
                    "deployment_ref": deployment_ref,
                },
                headers=_regulus_self_auth_headers(request),
            )
            resp.raise_for_status()
            data = resp.json()
            return DeploymentCostResponse(
                deployment_ref=deployment_ref,
                total_cost_usd=float(data.get("actual_spend_usd", 0)),
                paid_spend_usd=float(data.get("paid_spend_usd", 0)),
                estimated_spend_usd=float(data.get("estimated_spend_usd", 0)),
                unmeasured_spend_usd=float(data.get("unmeasured_spend_usd", 0)),
                active_exposure_usd=float(data.get("active_exposure_usd", 0)),
                ambiguous_exposure_usd=float(data.get("ambiguous_exposure_usd", 0)),
            )
        except httpx.HTTPError as exc:
            # A02-10: an httpx error's message carries the full URL it dialled,
            # which is the Regulus base URL -- internal infrastructure the caller
            # does not otherwise learn. Reported as a category instead.
            raise HTTPException(
                status_code=503,
                detail=safe_error_detail(exc, context="regulus backend"),
            ) from exc

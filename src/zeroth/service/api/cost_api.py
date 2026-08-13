"""Cost attribution REST API querying Regulus backend (per D-16).

Provides endpoints that return cumulative spend for tenants and
deployments by querying the Regulus backend as the source of truth.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from zeroth.integrations.http.factory import governed_async_client
from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
    require_resource_scope,
)


class TenantCostResponse(BaseModel):
    """Response for GET /v1/tenants/{tenant_id}/cost (per D-14)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    total_cost_usd: float
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
    currency: str = "USD"


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
            return TenantCostResponse(
                tenant_id=tenant_id,
                total_cost_usd=float(data.get("total_cost_usd", 0)),
                budget_cap_usd=data.get("budget_cap_usd"),
            )
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
            return TenantCostResponse(
                tenant_id=tenant_id,
                total_cost_usd=float(data.get("total_cost_usd", 0)),
                budget_cap_usd=data.get("budget_cap_usd"),
            )
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
        await require_permission(request, Permission.METRICS_READ)
        # Scope isolation (audit F4 follow-up): this service serves exactly one
        # deployment; only its owner may read its cost, and only for that ref.
        # Otherwise any admin could read an arbitrary deployment's spend by ref.
        bootstrap = getattr(request.app.state, "bootstrap", None)
        deployment = getattr(bootstrap, "deployment", None)
        if deployment is None or deployment_ref != getattr(deployment, "deployment_ref", None):
            raise HTTPException(status_code=404, detail="deployment not found")
        await require_deployment_scope(request, deployment)
        regulus_base_url = getattr(request.app.state, "regulus_base_url", None)
        regulus_timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            client = await _regulus_client(request, regulus_timeout)
            resp = await client.get(
                f"{regulus_base_url}/dashboard/kpis",
                params={"deployment_ref": deployment_ref},
                headers=_regulus_self_auth_headers(request),
            )
            resp.raise_for_status()
            data = resp.json()
            return DeploymentCostResponse(
                deployment_ref=deployment_ref,
                total_cost_usd=float(data.get("total_cost_usd", 0)),
            )
        except httpx.HTTPError as exc:
            # A02-10: an httpx error's message carries the full URL it dialled,
            # which is the Regulus base URL -- internal infrastructure the caller
            # does not otherwise learn. Reported as a category instead.
            raise HTTPException(
                status_code=503,
                detail=safe_error_detail(exc, context="regulus backend"),
            ) from exc

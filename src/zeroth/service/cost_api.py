"""Cost attribution REST API querying Regulus backend (per D-16).

Provides endpoints that return cumulative spend for tenants and
deployments by querying the Regulus backend as the source of truth.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict


class TenantCostResponse(BaseModel):
    """Response for GET /v1/tenants/{tenant_id}/cost (per D-14)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    total_cost_usd: float
    budget_cap_usd: float | None = None
    currency: str = "USD"


class DeploymentCostResponse(BaseModel):
    """Response for GET /v1/deployments/{deployment_ref}/cost (per D-15)."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    total_cost_usd: float
    currency: str = "USD"


def register_cost_routes(app: FastAPI) -> None:
    """Register cost attribution query routes on the FastAPI app."""

    @app.get("/v1/tenants/{tenant_id}/cost", response_model=TenantCostResponse)
    async def get_tenant_cost(request: Request, tenant_id: str) -> TenantCostResponse:
        """Return cumulative spend for a tenant (per D-14, D-16)."""
        regulus_base_url = getattr(app.state, "regulus_base_url", None)
        regulus_timeout = getattr(app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            async with httpx.AsyncClient(timeout=regulus_timeout) as client:
                resp = await client.get(
                    f"{regulus_base_url}/dashboard/kpis",
                    params={"tenant_id": tenant_id},
                )
                resp.raise_for_status()
                data = resp.json()
                return TenantCostResponse(
                    tenant_id=tenant_id,
                    total_cost_usd=float(data.get("total_cost_usd", 0)),
                    budget_cap_usd=data.get("budget_cap_usd"),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"Regulus backend error: {exc}") from exc

    @app.get(
        "/v1/deployments/{deployment_ref}/cost",
        response_model=DeploymentCostResponse,
    )
    async def get_deployment_cost(request: Request, deployment_ref: str) -> DeploymentCostResponse:
        """Return cumulative spend for a deployment (per D-15, D-16)."""
        regulus_base_url = getattr(app.state, "regulus_base_url", None)
        regulus_timeout = getattr(app.state, "regulus_timeout", 5.0)
        if regulus_base_url is None:
            raise HTTPException(status_code=503, detail="Regulus backend not configured")
        try:
            async with httpx.AsyncClient(timeout=regulus_timeout) as client:
                resp = await client.get(
                    f"{regulus_base_url}/dashboard/kpis",
                    params={"deployment_ref": deployment_ref},
                )
                resp.raise_for_status()
                data = resp.json()
                return DeploymentCostResponse(
                    deployment_ref=deployment_ref,
                    total_cost_usd=float(data.get("total_cost_usd", 0)),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"Regulus backend error: {exc}") from exc

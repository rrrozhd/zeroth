"""Console-facing proxy for the bundled Regulus economic dashboard (F7).

The econ plane (Regulus) dashboard suite ships mounted at ``/regulus`` but is
unreachable from the console: the mount's open JWT issuer is gated off
(``app.py`` 404s ``/regulus/**/auth/token``), so a browser cannot mint the
Bearer the dashboard routes require. These routes bridge that gap exactly the
way :mod:`zeroth.service.api.cost_api` does — server-side self-auth (Zeroth's
own API key + an in-process econ Admin JWT) to the in-process mount — exposing
the read-only dashboard views under the console's own ``/v1/econ/*`` surface
behind ``METRICS_READ``. Query params are forwarded verbatim.

Only additive, read-only GET views are proxied; nothing here lets a caller
write to the control plane.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request

from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.service.api.authorization import Permission, require_permission


def _regulus_self_auth_headers(request: Request) -> dict[str, str] | None:
    """Self-auth headers for calling the (possibly in-process/gated) Regulus mount.

    Returns ``None`` when no provider is configured (separate-process, unauth
    topology), matching :mod:`cost_api` so behavior is unchanged there.
    """
    provider = getattr(request.app.state, "regulus_self_auth_headers", None)
    return provider() if provider is not None else None


async def _dashboard_proxy(request: Request, path: str) -> Any:
    """Forward a read-only GET to the Regulus dashboard, self-authing to the mount."""
    base_url = getattr(request.app.state, "regulus_base_url", None)
    timeout = getattr(request.app.state, "regulus_timeout", 5.0)
    if base_url is None:
        raise HTTPException(status_code=503, detail="Regulus backend not configured")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{base_url}{path}",
                params=dict(request.query_params),
                headers=_regulus_self_auth_headers(request),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        # A02-10: same leak as the three cost_api sites -- an httpx error's
        # message carries the full URL it dialled, i.e. the internal Regulus
        # base URL and port.
        raise HTTPException(
            status_code=503,
            detail=safe_error_detail(exc, context="regulus backend"),
        ) from exc


# Read-only dashboard views proxied to the console. Each maps a console path
# suffix to the mount's dashboard path; all are GET, query-param passthrough.
_DASHBOARD_VIEWS: tuple[str, ...] = (
    "kpis",
    "top-creators",
    "capital-destroyers",
    "capability-ranking",
    "confidence-trend",
    "efficiency-trend",
    "calibration-trend",
    "action-suppression",
    "data-quality-mix",
)


def register_econ_dashboard_routes(app: FastAPI | APIRouter) -> None:
    """Register console-facing econ dashboard proxy routes under ``/econ/dashboard/*``."""

    def _make(view: str):
        async def _handler(request: Request) -> Any:
            await require_permission(request, Permission.METRICS_READ)
            return await _dashboard_proxy(request, f"/dashboard/{view}")

        return _handler

    for view in _DASHBOARD_VIEWS:
        app.add_api_route(
            f"/econ/dashboard/{view}",
            _make(view),
            methods=["GET"],
            name=f"econ_dashboard_{view.replace('-', '_')}",
        )

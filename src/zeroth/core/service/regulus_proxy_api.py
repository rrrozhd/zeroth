"""Admin-gated console proxy to the bundled Regulus control plane.

The operator console holds only Zeroth's ``X-API-Key`` and, by design, cannot
obtain an econ-plane JWT (the issuer ``/regulus/**/auth/token`` is blocked over
HTTP to prevent any principal self-escalating to econ Admin -- see
``app.py`` / SECURITY.md). Regulus data is also *global* (capability-centric,
not tenant-partitioned), so it cannot be scoped per-tenant.

This module is the single, controlled path that brings Regulus data into the
console: a router mounted under ``/v1/econ/regulus`` that

1. requires the admin-tier :attr:`Permission.ECON_ADMIN` (platform-admin only);
2. mints the econ Admin token **in-process** via
   ``app.state.regulus_self_auth_headers`` (the HTTP issuer stays hidden);
3. forwards a fixed allowlist of **read-only GETs**, plus the enforcement
   **approve/reject** POSTs, to the in-process ``/regulus`` mount; and
4. returns the upstream JSON verbatim.

No registry/capability writes, no budget writes, no token issuance. See
``.planning/console-rebuild/REGULUS-FINDINGS.md``.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from zeroth.core.service.authorization import Permission, require_permission

# GET paths (relative to the regulus base, i.e. ``/regulus/v1``) the console may
# read. Prefix-matched. Deliberately excludes ``auth/`` (token issuance / login)
# and every write route.
_GET_ALLOW_PREFIXES: tuple[str, ...] = (
    "dashboard/",
    "registry/",
    "capabilities",
    "evaluations/",
    "enforcement/actions",
    "enforcement/policy-actions",
    "costing/",
    "reconciliation/",
    "performance/",
    "outcomes/query",
    "budget/status",
    "metrics",
)


def _self_headers(request: Request) -> dict[str, str] | None:
    provider = getattr(request.app.state, "regulus_self_auth_headers", None)
    return provider() if provider is not None else None


def _regulus_base(request: Request) -> str | None:
    return getattr(request.app.state, "regulus_base_url", None)


def _get_allowed(path: str) -> bool:
    # Reject path traversal / absolute paths before prefix-matching.
    if ".." in path or path.startswith("/"):
        return False
    if path.startswith("auth/"):
        return False
    return any(path == p or path.startswith(p) for p in _GET_ALLOW_PREFIXES)


def _passthrough(resp: httpx.Response) -> JSONResponse:
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=content)


def register_regulus_proxy_routes(app: FastAPI | APIRouter) -> None:
    """Register the admin-gated Regulus read/enforcement proxy on ``app``."""

    @app.get("/econ/regulus/{path:path}")
    async def regulus_read(request: Request, path: str) -> JSONResponse:
        await require_permission(request, Permission.ECON_ADMIN)
        base = _regulus_base(request)
        if base is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Regulus not enabled",
            )
        if not _get_allowed(path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{base}/{path}",
                    params=dict(request.query_params),
                    headers=_self_headers(request),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Regulus unreachable: {exc}",
            ) from exc
        return _passthrough(resp)

    @app.post("/econ/regulus/enforcement/actions/{action_id}/{decision}")
    async def regulus_enforcement_decision(
        request: Request, action_id: int, decision: str
    ) -> JSONResponse:
        await require_permission(request, Permission.ECON_ADMIN)
        if decision not in ("approve", "reject"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        base = _regulus_base(request)
        if base is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Regulus not enabled",
            )
        body = await request.body()
        headers = {**(_self_headers(request) or {}), "Content-Type": "application/json"}
        timeout = getattr(request.app.state, "regulus_timeout", 5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base}/enforcement/actions/{action_id}/{decision}",
                    content=body,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Regulus unreachable: {exc}",
            ) from exc
        return _passthrough(resp)

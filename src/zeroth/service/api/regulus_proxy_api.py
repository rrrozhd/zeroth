"""Narrow, platform-admin-only proxy for the Regulus console surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from zeroth.service.api.authorization import Permission, require_permission

_PREFIX = "/econ/regulus"
_MAX_BODY_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class RegulusRoute:
    """One explicitly allowed console-to-Regulus transition."""

    name: str
    method: Literal["GET", "POST"]
    console_path: str


def _get(name: str, path: str) -> RegulusRoute:
    return RegulusRoute(name=name, method="GET", console_path=path)


ROUTES: tuple[RegulusRoute, ...] = (
    _get("regulus_dashboard_kpis", "/dashboard/kpis"),
    _get("regulus_dashboard_top_creators", "/dashboard/top-creators"),
    _get("regulus_dashboard_capital_destroyers", "/dashboard/capital-destroyers"),
    _get("regulus_dashboard_capability_ranking", "/dashboard/capability-ranking"),
    _get("regulus_dashboard_confidence_trend", "/dashboard/confidence-trend"),
    _get("regulus_dashboard_efficiency_trend", "/dashboard/efficiency-trend"),
    _get("regulus_dashboard_calibration_trend", "/dashboard/calibration-trend"),
    _get("regulus_dashboard_action_suppression", "/dashboard/action-suppression"),
    _get("regulus_dashboard_confidence_gate", "/dashboard/confidence-gate-status"),
    _get("regulus_dashboard_data_quality", "/dashboard/data-quality-mix"),
    _get("regulus_dashboard_policy_timeline", "/dashboard/policy-timeline"),
    _get("regulus_dashboard_drift_timeline", "/dashboard/drift-timeline/{capability_id}"),
    _get(
        "regulus_dashboard_implementation_compare",
        "/dashboard/implementation-compare/{capability_id}",
    ),
    _get("regulus_registry_capabilities", "/registry/capabilities"),
    _get("regulus_registry_capability", "/registry/capabilities/{capability_id}"),
    _get("regulus_registry_implementation", "/registry/implementations/{implementation_id}"),
    _get("regulus_evaluations_latest", "/evaluations/{capability_id}/latest"),
    _get("regulus_evaluations_history", "/evaluations/{capability_id}/history"),
    _get("regulus_enforcement_actions", "/enforcement/actions"),
    _get("regulus_enforcement_policy_actions", "/enforcement/policy-actions"),
    RegulusRoute("regulus_enforcement_approve", "POST", "/enforcement/actions/{action_id}/approve"),
    RegulusRoute("regulus_enforcement_reject", "POST", "/enforcement/actions/{action_id}/reject"),
    _get("regulus_costing_profile", "/costing/profiles/{profile_id}"),
    _get("regulus_costing_estimate", "/costing/estimates/{capability_id}/latest"),
    _get("regulus_performance_summary", "/performance/summary"),
    _get("regulus_performance_capabilities", "/performance/capabilities"),
    _get("regulus_reconciliation_calibration", "/reconciliation/calibration-summary"),
)


class _ActionBody(BaseModel):
    """Only body accepted by approve/reject transitions."""

    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _bounded_reason(cls, value: str) -> str:
        if len(value) > 2_000:
            raise ValueError("reason exceeds 2000 characters")
        return value


def _validate_canonical_request(request: Request) -> None:
    raw_path = request.scope.get("raw_path", b"")
    lowered = raw_path.lower()
    if any(marker in lowered for marker in (b"%2e", b"%2f", b"%5c", b"%00")):
        raise HTTPException(status_code=400, detail="non-canonical Regulus path")
    if b"\\" in raw_path or b"//" in raw_path or any(byte < 0x20 for byte in raw_path):
        raise HTTPException(status_code=400, detail="non-canonical Regulus path")
    if any(segment in {".", ".."} for segment in request.url.path.split("/")):
        raise HTTPException(status_code=400, detail="non-canonical Regulus path")
    if request.url.query:
        raise HTTPException(status_code=400, detail="unexpected Regulus query")


async def _action_body(request: Request) -> dict[str, str]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        payload = json.loads(raw)
        return _ActionBody.model_validate(payload).model_dump()
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid request body") from exc


async def _forward(request: Request, route: RegulusRoute) -> JSONResponse:
    base_url = getattr(request.app.state, "regulus_base_url", None)
    provider = getattr(request.app.state, "regulus_self_auth_headers", None)
    headers = provider() if provider is not None else None
    if not base_url or not headers:
        raise HTTPException(status_code=503, detail="Regulus backend unavailable")

    suffix = route.console_path
    for name, value in request.path_params.items():
        suffix = suffix.replace(f"{{{name}}}", str(value))
    body = await _action_body(request) if route.method == "POST" else None
    timeout = getattr(request.app.state, "regulus_timeout", 5.0)
    transport = getattr(request.app.state, "regulus_transport", None)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                route.method,
                f"{str(base_url).rstrip('/')}{suffix}",
                headers=headers,
                json=body,
            )
        if not 200 <= response.status_code < 300:
            raise HTTPException(status_code=502, detail="Regulus backend request failed")
        return JSONResponse(content=response.json(), status_code=response.status_code)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Regulus backend request failed") from exc


def register_regulus_proxy_routes(app: FastAPI | APIRouter) -> None:
    """Register the explicit, versioned Regulus proxy route table."""

    def handler_for(route: RegulusRoute):
        async def handler(request: Request) -> JSONResponse:
            await require_permission(request, Permission.ECON_ADMIN)
            _validate_canonical_request(request)
            return await _forward(request, route)

        return handler

    for route in ROUTES:
        app.add_api_route(
            f"{_PREFIX}{route.console_path}",
            handler_for(route),
            methods=[route.method],
            name=route.name,
        )

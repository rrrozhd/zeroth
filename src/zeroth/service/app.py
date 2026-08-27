"""FastAPI application factory for the deployment wrapper."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zeroth.platform.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from zeroth.service.api.approval_api import register_approval_routes
from zeroth.service.api.artifact_api import register_artifact_routes
from zeroth.service.api.audit_api import register_audit_routes
from zeroth.service.api.authentication import AuthenticationError, record_service_denial
from zeroth.service.api.certification_api import register_certification_routes
from zeroth.service.api.console_ui import console_cors_origins, mount_console
from zeroth.service.api.contracts_api import register_contract_routes
from zeroth.service.api.cost_api import register_cost_routes
from zeroth.service.api.econ_analytics_api import register_econ_analytics_routes
from zeroth.service.api.econ_dashboard_api import register_econ_dashboard_routes
from zeroth.service.api.enforcement_api import register_enforcement_routes
from zeroth.service.api.guardrail_api import register_guardrail_routes
from zeroth.service.api.health import (
    HealthResponse,
    audit_delivery_health,
    certification_readiness,
    langgraph_gateway_health,
)
from zeroth.service.api.identity_api import register_identity_routes
from zeroth.service.api.langgraph_enforcement_api import register_langgraph_enforcement_routes
from zeroth.service.api.operation_api import register_operation_routes
from zeroth.service.api.regulus_proxy_api import register_regulus_proxy_routes
from zeroth.service.api.retention_api import register_retention_routes
from zeroth.service.api.rightsizing_api import register_rightsizing_routes
from zeroth.service.api.route_authorization import authorize_matched_route
from zeroth.service.api.run_api import register_run_routes
from zeroth.service.api.template_api import register_template_routes
from zeroth.service.api.webhook_api import register_webhook_routes
from zeroth.service.bootstrap.lifecycle import service_lifespan

logger = logging.getLogger(__name__)

_PUBLIC_HEALTH_PATHS = frozenset({"/health", "/health/live", "/health/ready"})
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
        "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self' http: https: ws: wss:"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ServiceBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the HTTP app."""

    deployment: object
    graph: object
    contract_registry: object
    approval_service: object
    run_repository: object
    orchestrator: object
    audit_repository: object
    authenticator: object


def create_app(
    bootstrap: ServiceBootstrapLike,
    *,
    extra_v1_route_registrars: Sequence[Callable[[APIRouter], None]] = (),
) -> FastAPI:
    """Create the service API for a single deployment."""
    app = FastAPI(
        title="Zeroth Platform API",
        description="Governed medium-code platform for production-grade multi-agent systems",
        version="1.0.0",
        lifespan=service_lifespan,
    )
    app.state.bootstrap = bootstrap

    # Regulus backend URL for cost API queries (per D-16).
    regulus_client = getattr(bootstrap, "regulus_client", None)
    if regulus_client is not None:
        from zeroth.platform.config.settings import get_settings

        _regulus_settings = get_settings().regulus
        app.state.regulus_base_url = _regulus_settings.base_url
        app.state.regulus_timeout = _regulus_settings.request_timeout
        app.state.regulus_registration_ready = False

        # Self-auth headers for Zeroth's own calls to the (possibly in-process,
        # gated) Regulus mount: Zeroth's first service API key + a fresh
        # econ_plane Admin JWT. Used by the cost API; the SDK client and budget
        # enforcer get the same provider via bootstrap.
        from zeroth.econ.analytics.service_auth import make_self_auth_headers_provider

        _auth_cfg = getattr(bootstrap, "auth_config", None)
        _self_api_key = _auth_cfg.api_keys[0].secret if _auth_cfg and _auth_cfg.api_keys else None
        app.state.regulus_self_auth_headers = make_self_auth_headers_provider(_self_api_key)

        # Mount the Regulus economic control plane in-process under /regulus.
        # External requests cross Zeroth's API-key gate and Regulus JWT auth;
        # trusted self-calls dispatch directly through ASGI and still carry the
        # Regulus service JWT. A configured external backend remains the
        # fallback when the bundled plane is unavailable.
        try:
            from zeroth.econ.plane.main import app as econ_plane_app

            app.mount("/regulus", econ_plane_app)
            app.state.regulus_base_url = "http://regulus.internal/v1"
            app.state.regulus_transport = httpx.ASGITransport(app=econ_plane_app)
            logger.info("Mounted bundled Regulus control plane at /regulus")
        except ImportError:
            logger.warning(
                "Regulus is enabled but econ_plane is not importable; install the "
                "'regulus' extra (uv sync --extra regulus) to mount it in-process."
            )

    # Register health probe routes BEFORE auth middleware (per D-07).
    from zeroth.service.api.health import register_health_routes

    register_health_routes(app)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        # Bypass authentication for:
        #  - /health probes (load balancers),
        #  - the /console static UI (browser navigation can't send the API key;
        #    the UI's own /v1 and /api/studio fetches still carry it).
        # Valid configured CORS preflights are answered by the outermost
        # CORSMiddleware before they reach this authentication boundary.
        path = request.url.path
        if path in _PUBLIC_HEALTH_PATHS or path == "/console" or path.startswith("/console/"):
            cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
            set_correlation_id(cid)
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = get_correlation_id()
            return response

        # Block the bundled control plane's open token issuer over HTTP. econ_plane
        # mints an Admin JWT for any caller of POST /**/auth/token with no credential
        # check of its own; once mounted, that would let any authenticated Zeroth
        # principal escalate to econ Admin (and read cross-tenant KPIs). Zeroth's own
        # self-calls mint their econ token in-process (service_auth.mint_econ_service_token),
        # never via this endpoint, so blocking it breaks nothing internal. Return 404 so
        # the issuer is simply absent from the HTTP surface. See SECURITY.md.
        if path.startswith("/regulus/") and path.rstrip("/").endswith("/auth/token"):
            cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
            set_correlation_id(cid)
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # Propagate or generate a correlation ID for the lifetime of this request.
        cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
        set_correlation_id(cid)
        bootstrap = app.state.bootstrap
        try:
            request.state.principal = await asyncio.to_thread(
                bootstrap.authenticator.authenticate_headers, request.headers
            )
        except AuthenticationError as exc:
            logger.info("authentication failed: %s", exc)
            await record_service_denial(
                audit_repository=getattr(bootstrap, "audit_repository", None),
                deployment=getattr(bootstrap, "deployment", None),
                request=request,
                node_id="service.auth",
                status="unauthenticated",
                error=str(exc),
            )
            return JSONResponse(
                status_code=401,
                content={"detail": str(exc)},
            )
        try:
            await authorize_matched_route(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = get_correlation_id()
        return response

    @app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
    async def health() -> HealthResponse:
        deployment = app.state.bootstrap.deployment
        certification = await certification_readiness(app.state.bootstrap)
        return HealthResponse(
            deployment_ref=deployment.deployment_ref,
            deployment_version=deployment.version,
            graph_version_ref=deployment.graph_version_ref,
            campaign_id=getattr(app.state.bootstrap, "evaluation_campaign_id", None),
            langgraph_gateway=langgraph_gateway_health(app.state.bootstrap),
            audit_delivery=audit_delivery_health(app.state.bootstrap),
            production_ready=certification.production_ready,
            certification=certification,
        )

    # Primary: versioned routes under /v1/ (per D-06)
    v1_router = APIRouter(prefix="/v1", tags=["v1"])
    register_contract_routes(v1_router)
    register_certification_routes(v1_router)
    register_audit_routes(v1_router)
    register_approval_routes(v1_router)
    register_run_routes(v1_router)
    register_operation_routes(v1_router)
    register_identity_routes(v1_router)
    register_guardrail_routes(v1_router)

    # Studio graph authoring API
    from zeroth.service.api.studio_api import router as studio_router

    app.include_router(studio_router)

    from zeroth.service.api.admin_api import register_admin_routes

    register_admin_routes(v1_router)
    register_cost_routes(v1_router)
    register_rightsizing_routes(v1_router)
    register_econ_analytics_routes(v1_router)
    register_econ_dashboard_routes(v1_router)
    register_webhook_routes(v1_router)
    register_artifact_routes(v1_router)
    register_template_routes(v1_router)
    register_retention_routes(v1_router)
    register_enforcement_routes(v1_router)
    register_langgraph_enforcement_routes(v1_router)
    register_regulus_proxy_routes(v1_router)

    from zeroth.service.api.connector_api import register_connector_routes
    from zeroth.service.api.deployment_api import register_deployment_routes
    from zeroth.service.api.manifest_api import register_manifest_routes

    register_deployment_routes(v1_router)
    register_connector_routes(v1_router)
    register_manifest_routes(v1_router)
    for registrar in extra_v1_route_registrars:
        registrar(v1_router)

    app.include_router(v1_router)

    # Mount the static console before the gateway catch-all so native console
    # navigation remains authoritative.
    mount_console(app)

    # The optional Agent Server gateway owns root compatibility paths after all
    # native/versioned surfaces, but before the unversioned Zeroth aliases.
    gateway_proxy = getattr(bootstrap, "langgraph_gateway_proxy", None)
    gateway_websocket_handler = getattr(bootstrap, "langgraph_gateway_websocket_handler", None)
    if gateway_proxy is not None and gateway_websocket_handler is not None:
        from zeroth.service.langgraph_gateway.routes import register_gateway_routes

        register_gateway_routes(
            app,
            proxy=gateway_proxy,
            websocket_handler=gateway_websocket_handler,
            authenticator=bootstrap.authenticator,
            compatibility=getattr(bootstrap, "langgraph_gateway_compatibility", None),
        )

    # Backward-compatible aliases: same routes without /v1/ prefix,
    # excluded from OpenAPI spec to avoid duplicate operationIds (per D-06, Pitfall 3)
    compat_router = APIRouter(include_in_schema=False)
    register_contract_routes(compat_router)
    register_certification_routes(compat_router)
    register_audit_routes(compat_router)
    register_approval_routes(compat_router)
    register_run_routes(compat_router)
    register_operation_routes(compat_router)
    register_identity_routes(compat_router)
    register_guardrail_routes(compat_router)
    register_admin_routes(compat_router)
    register_cost_routes(compat_router)
    register_rightsizing_routes(compat_router)
    register_econ_analytics_routes(compat_router)
    register_econ_dashboard_routes(compat_router)
    register_webhook_routes(compat_router)
    register_artifact_routes(compat_router)
    register_template_routes(compat_router)
    register_retention_routes(compat_router)
    register_enforcement_routes(compat_router)
    register_deployment_routes(compat_router)
    register_connector_routes(compat_router)
    register_manifest_routes(compat_router)

    app.include_router(compat_router)

    # Standalone-console support: enable CORS only when origins are configured
    # (mounted mode is same-origin and needs none). Added last so it sits
    # OUTERMOST and answers OPTIONS preflight before the auth middleware.
    cors_origins = console_cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=[
                "X-API-Key",
                "X-Tenant-ID",
                "Content-Type",
                "Accept",
                "X-Correlation-ID",
            ],
            allow_credentials=False,
        )

    # Registered last so every API, auth, CORS, and mounted-console response
    # passes through the same browser-security boundary.
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled request failure: %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    return app


_create_app_parameters = inspect.signature(create_app).parameters
create_app.__signature__ = inspect.signature(create_app).replace(
    parameters=[
        parameter
        for name, parameter in _create_app_parameters.items()
        if name != "extra_v1_route_registrars"
    ]
)

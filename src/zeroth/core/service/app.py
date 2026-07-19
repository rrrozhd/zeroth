"""FastAPI application factory for the deployment wrapper."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import secrets
import signal
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from zeroth.core.service.approval_api import register_approval_routes
from zeroth.core.service.audit_api import register_audit_routes
from zeroth.core.service.auth import AuthenticationError, record_service_denial
from zeroth.core.service.console_ui import console_cors_origins, mount_console
from zeroth.core.service.retention_api import register_retention_routes
from zeroth.core.service.rightsizing_api import register_rightsizing_routes
from zeroth.core.service.template_api import register_template_routes
from zeroth.core.service.webhook_api import register_webhook_routes
from zeroth.service.api.artifact_api import register_artifact_routes
from zeroth.service.api.contracts_api import register_contract_routes
from zeroth.service.api.cost_api import register_cost_routes
from zeroth.service.api.econ_analytics_api import register_econ_analytics_routes
from zeroth.service.api.run_api import register_run_routes

logger = logging.getLogger(__name__)


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


class HealthResponse(BaseModel):
    """Response payload for the wrapper health endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="ok")
    deployment_ref: str
    deployment_version: int
    graph_version_ref: str


def create_app(bootstrap: ServiceBootstrapLike) -> FastAPI:
    """Create the service API for a single deployment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # When the bundled Regulus control plane is mounted in-process, initialize
        # its own schema + seed data here: Starlette does not run a mounted
        # sub-app's startup events, so econ_plane.main's on_startup never fires.
        if getattr(app.state.bootstrap, "regulus_client", None) is not None:
            try:
                from zeroth.econ_plane.common.bootstrap import bootstrap as econ_plane_bootstrap
                from zeroth.econ_plane.config import settings as ecp_settings
                from zeroth.econ_plane.connectors.service import init_otel_metrics

                # Default-safe JWT secret for the bundled control plane. The
                # mounted plane signs its Admin tokens with ECP_JWT_SECRET, so
                # booting on the shipped placeholder 'change-me' would make those
                # tokens forgeable. The plane is now enabled by default (G1), so
                # rather than CRASH a fresh deploy we auto-generate a
                # cryptographically-strong per-process secret and use it: this is
                # STRONGER than the placeholder (tokens stay unforgeable — the
                # whole point of the v0.4 guard) and needs no operator action.
                #
                # The secret is assigned onto the module-level econ_plane settings
                # singleton BEFORE any token is minted or verified, so BOTH the
                # self-auth mint (econ.service_auth.mint_econ_service_token) AND the
                # mount's token verify (econ_plane.auth.service.decode_token) — each
                # of which reads ``settings.jwt_secret`` at call time off this same
                # singleton — observe the ephemeral value.
                #
                # Escapes (unchanged):
                #  - explicit ECP_JWT_SECRET (any non-placeholder value) -> used as-is;
                #  - ECP_ALLOW_INSECURE_JWT_SECRET=1 -> keep the literal 'change-me'
                #    placeholder (tests / deliberately-insecure local dev).
                #
                # Per-process is safe here: the ONLY client of /regulus is Zeroth's
                # own in-process self-auth in THIS process (the open token issuer is
                # blocked at the gate), so a cross-worker secret mismatch is not a
                # reachable path. Set an explicit ECP_JWT_SECRET for multi-worker or
                # persistent deployments. See SECURITY.md.
                if ecp_settings.jwt_secret == "change-me" and os.environ.get(
                    "ECP_ALLOW_INSECURE_JWT_SECRET"
                ) not in ("1", "true", "yes"):
                    ecp_settings.jwt_secret = secrets.token_urlsafe(32)
                    logger.warning(
                        "Using an ephemeral per-process Regulus signing secret; set "
                        "ECP_JWT_SECRET for multi-worker or persistent deployments."
                    )

                econ_plane_bootstrap()
                init_otel_metrics()  # no-op unless ECP_OTEL_METRICS_ENABLED
                logger.info("Initialized bundled Regulus control plane")
            except ImportError:
                pass

        worker = getattr(app.state.bootstrap, "worker", None)
        poll_task: asyncio.Task | None = None
        queue_gauge_task: asyncio.Task | None = None
        delivery_poll_task: asyncio.Task | None = None
        sla_checker_task: asyncio.Task | None = None

        if worker is not None:
            await worker.start()
            poll_task = asyncio.create_task(worker.poll_loop(), name="worker-poll")

        # Start queue depth gauge if observability is wired.
        queue_gauge = getattr(app.state.bootstrap, "queue_gauge", None)
        if queue_gauge is not None:
            queue_gauge_task = asyncio.create_task(queue_gauge.run(), name="queue-gauge")

        # Start webhook delivery worker if configured.
        delivery_worker = getattr(app.state.bootstrap, "delivery_worker", None)
        if delivery_worker is not None:
            delivery_poll_task = asyncio.create_task(
                delivery_worker.poll_loop(), name="webhook-delivery"
            )

        # Start approval SLA checker if configured.
        sla_checker = getattr(app.state.bootstrap, "sla_checker", None)
        if sla_checker is not None:
            sla_checker_task = asyncio.create_task(sla_checker.poll_loop(), name="sla-checker")

        # WS-E: start the retention purge worker when enabled (mirrors the SLA
        # checker exactly). None unless ZEROTH_RETENTION__ENABLED is true.
        retention_worker_task: asyncio.Task | None = None
        retention_worker = getattr(app.state.bootstrap, "retention_worker", None)
        if retention_worker is not None:
            retention_worker_task = asyncio.create_task(
                retention_worker.poll_loop(), name="retention-purge"
            )

        # Phase 16: ARQ wakeup consumer task.
        arq_consumer_task: asyncio.Task | None = None
        arq_pool = getattr(app.state.bootstrap, "arq_pool", None)
        if worker is not None and arq_pool is not None:
            try:
                from zeroth.core.config.settings import get_settings
                from zeroth.core.dispatch.arq_wakeup import run_arq_consumer

                redis_settings = get_settings().redis
                arq_consumer_task = asyncio.create_task(
                    run_arq_consumer(redis_settings, worker.handle_wakeup),
                    name="arq-consumer",
                )
            except ImportError:
                pass

        # Phase 16: SIGTERM / SIGINT graceful shutdown signal handler.
        shutdown_event = asyncio.Event()

        def _handle_shutdown_signal() -> None:
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, _handle_shutdown_signal)
            loop.add_signal_handler(signal.SIGINT, _handle_shutdown_signal)
        except (NotImplementedError, RuntimeError):
            pass  # Signal handlers not supported on this platform (e.g., Windows)

        async def _shutdown_watcher() -> None:
            await shutdown_event.wait()
            if worker is not None:
                await worker.graceful_shutdown()

        shutdown_watcher_task: asyncio.Task | None = None
        if worker is not None:
            shutdown_watcher_task = asyncio.create_task(
                _shutdown_watcher(), name="shutdown-watcher"
            )

        yield

        # Cancel shutdown watcher.
        if shutdown_watcher_task is not None:
            shutdown_watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_watcher_task

        # Phase 16: Graceful shutdown -- wait for in-flight runs then release leases.
        if worker is not None:
            await worker.graceful_shutdown()

        # Cancel ARQ consumer.
        if arq_consumer_task is not None:
            arq_consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await arq_consumer_task

        # Close ARQ pool.
        if arq_pool is not None:
            with contextlib.suppress(Exception):
                await arq_pool.close()

        # Graceful shutdown: cancel the poll loop (not the executing runs).
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        if queue_gauge_task is not None:
            queue_gauge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_gauge_task

        # Shutdown webhook delivery worker.
        if delivery_poll_task is not None:
            delivery_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delivery_poll_task

        # Shutdown SLA checker.
        if sla_checker_task is not None:
            sla_checker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sla_checker_task

        # Shutdown retention purge worker.
        if retention_worker_task is not None:
            retention_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_worker_task

        # Close webhook HTTP client.
        webhook_http_client = getattr(app.state.bootstrap, "webhook_http_client", None)
        if webhook_http_client is not None:
            await webhook_http_client.aclose()

        # Flush and stop Regulus telemetry transport (Pitfall 2).
        regulus_client = getattr(app.state.bootstrap, "regulus_client", None)
        if regulus_client is not None:
            regulus_client.stop()

        # Close the shared secret provider's pooled HTTP client (Vault). The
        # lifespan is the single owner of this shutdown: entrypoints and
        # bootstrap never close it, so it happens exactly once.
        secret_provider = getattr(app.state.bootstrap, "secret_provider", None)
        provider_aclose = getattr(secret_provider, "aclose", None)
        if callable(provider_aclose):
            close_result = provider_aclose()
            if inspect.isawaitable(close_result):
                await close_result

    app = FastAPI(
        title="Zeroth Platform API",
        description="Governed medium-code platform for production-grade multi-agent systems",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.bootstrap = bootstrap

    # Regulus backend URL for cost API queries (per D-16).
    regulus_client = getattr(bootstrap, "regulus_client", None)
    if regulus_client is not None:
        from zeroth.core.config.settings import get_settings

        _regulus_settings = get_settings().regulus
        app.state.regulus_base_url = _regulus_settings.base_url
        app.state.regulus_timeout = _regulus_settings.request_timeout

        # Self-auth headers for Zeroth's own calls to the (possibly in-process,
        # gated) Regulus mount: Zeroth's first service API key + a fresh
        # econ_plane Admin JWT. Used by the cost API; the SDK client and budget
        # enforcer get the same provider via bootstrap.
        from zeroth.core.econ.service_auth import make_self_auth_headers_provider

        _auth_cfg = getattr(bootstrap, "auth_config", None)
        _self_api_key = _auth_cfg.api_keys[0].secret if _auth_cfg and _auth_cfg.api_keys else None
        app.state.regulus_self_auth_headers = make_self_auth_headers_provider(_self_api_key)

        # Mount the Regulus economic control plane in-process under
        # /regulus (source at src/zeroth/econ_plane). Guarded so a plain
        # `zeroth-core` install without the `regulus` extra still boots. The
        # mount sits behind Zeroth's API-key auth (no bypass); econ_plane then
        # enforces its own JWT on protected routes. Zeroth's econ client/budget/
        # cost reach it over HTTP via ZEROTH_REGULUS__BASE_URL (point at
        # http://<host>/regulus/v1 in-process), carrying both credentials so the
        # fail-open boundary (D-12) is preserved. The backend keeps its own
        # ECP_-prefixed settings, database, and JWT auth.
        try:
            from zeroth.econ_plane.main import app as econ_plane_app

            app.mount("/regulus", econ_plane_app)
            logger.info("Mounted bundled Regulus control plane at /regulus")
        except ImportError:
            logger.warning(
                "Regulus is enabled but econ_plane is not importable; install the "
                "'regulus' extra (uv sync --extra regulus) to mount it in-process."
            )

    # Register health probe routes BEFORE auth middleware (per D-07).
    from zeroth.core.service.health import register_health_routes

    register_health_routes(app)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        # Bypass authentication for:
        #  - /health probes (load balancers),
        #  - the /console static UI (browser navigation can't send the API key;
        #    the UI's own /v1 and /api/studio fetches still carry it),
        #  - CORS preflight (OPTIONS carries no API key); CORSMiddleware runs
        #    outermost and answers these before they reach here.
        path = request.url.path
        if (
            path.startswith("/health")
            or path == "/console"
            or path.startswith("/console/")
            or request.method == "OPTIONS"
        ):
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
            request.state.principal = bootstrap.authenticator.authenticate_headers(request.headers)
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
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = get_correlation_id()
        return response

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        deployment = app.state.bootstrap.deployment
        return HealthResponse(
            deployment_ref=deployment.deployment_ref,
            deployment_version=deployment.version,
            graph_version_ref=deployment.graph_version_ref,
        )

    # Primary: versioned routes under /v1/ (per D-06)
    v1_router = APIRouter(prefix="/v1", tags=["v1"])
    register_contract_routes(v1_router)
    register_audit_routes(v1_router)
    register_approval_routes(v1_router)
    register_run_routes(v1_router)

    # Studio graph authoring API
    from zeroth.core.service.studio_api import router as studio_router

    app.include_router(studio_router)

    from zeroth.service.api.admin_api import register_admin_routes

    register_admin_routes(v1_router)
    register_cost_routes(v1_router)
    register_rightsizing_routes(v1_router)
    register_econ_analytics_routes(v1_router)
    register_webhook_routes(v1_router)
    register_artifact_routes(v1_router)
    register_template_routes(v1_router)
    register_retention_routes(v1_router)

    from zeroth.core.service.connector_api import register_connector_routes
    from zeroth.core.service.deployment_api import register_deployment_routes
    from zeroth.service.api.manifest_api import register_manifest_routes

    register_deployment_routes(v1_router)
    register_connector_routes(v1_router)
    register_manifest_routes(v1_router)

    app.include_router(v1_router)

    # Backward-compatible aliases: same routes without /v1/ prefix,
    # excluded from OpenAPI spec to avoid duplicate operationIds (per D-06, Pitfall 3)
    compat_router = APIRouter(include_in_schema=False)
    register_contract_routes(compat_router)
    register_audit_routes(compat_router)
    register_approval_routes(compat_router)
    register_run_routes(compat_router)
    register_admin_routes(compat_router)
    register_cost_routes(compat_router)
    register_rightsizing_routes(compat_router)
    register_econ_analytics_routes(compat_router)
    register_webhook_routes(compat_router)
    register_artifact_routes(compat_router)
    register_template_routes(compat_router)
    register_retention_routes(compat_router)
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
            allow_headers=["X-API-Key", "Content-Type", "Accept", "X-Correlation-ID"],
            allow_credentials=False,
        )

    # Mount the static console UI at /console when a build is present.
    mount_console(app)

    return app

"""FastAPI application factory for the deployment wrapper."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from zeroth.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from zeroth.service.approval_api import register_approval_routes
from zeroth.service.audit_api import register_audit_routes
from zeroth.service.auth import AuthenticationError, record_service_denial
from zeroth.service.contracts_api import register_contract_routes
from zeroth.service.cost_api import register_cost_routes
from zeroth.service.run_api import register_run_routes
from zeroth.service.webhook_api import register_webhook_routes


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
            sla_checker_task = asyncio.create_task(
                sla_checker.poll_loop(), name="sla-checker"
            )

        # Phase 16: ARQ wakeup consumer task.
        arq_consumer_task: asyncio.Task | None = None
        arq_pool = getattr(app.state.bootstrap, "arq_pool", None)
        if worker is not None and arq_pool is not None:
            try:
                from zeroth.config.settings import get_settings
                from zeroth.dispatch.arq_wakeup import run_arq_consumer

                redis_settings = get_settings().redis
                arq_consumer_task = asyncio.create_task(
                    run_arq_consumer(redis_settings, worker.handle_wakeup),
                    name="arq-consumer",
                )
            except ImportError:
                pass

        # Phase 16: SIGTERM / SIGINT graceful shutdown signal handler.
        shutdown_event = asyncio.Event()

        def _handle_shutdown_signal():
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, _handle_shutdown_signal)
            loop.add_signal_handler(signal.SIGINT, _handle_shutdown_signal)
        except (NotImplementedError, RuntimeError):
            pass  # Signal handlers not supported on this platform (e.g., Windows)

        async def _shutdown_watcher():
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

        # Close webhook HTTP client.
        webhook_http_client = getattr(
            app.state.bootstrap, "webhook_http_client", None
        )
        if webhook_http_client is not None:
            await webhook_http_client.aclose()

        # Flush and stop Regulus telemetry transport (Pitfall 2).
        regulus_client = getattr(app.state.bootstrap, "regulus_client", None)
        if regulus_client is not None:
            regulus_client.stop()

    app = FastAPI(title="Zeroth Service Wrapper", lifespan=lifespan)
    app.state.bootstrap = bootstrap

    # Regulus backend URL for cost API queries (per D-16).
    regulus_client = getattr(bootstrap, "regulus_client", None)
    if regulus_client is not None:
        from zeroth.config.settings import get_settings

        _regulus_settings = get_settings().regulus
        app.state.regulus_base_url = _regulus_settings.base_url
        app.state.regulus_timeout = _regulus_settings.request_timeout

    # Register health probe routes BEFORE auth middleware (per D-07).
    from zeroth.service.health import register_health_routes

    register_health_routes(app)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        # Health endpoints bypass authentication (load balancer probes).
        if request.url.path.startswith("/health"):
            cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
            set_correlation_id(cid)
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = get_correlation_id()
            return response

        # Propagate or generate a correlation ID for the lifetime of this request.
        cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
        set_correlation_id(cid)
        bootstrap = app.state.bootstrap
        try:
            request.state.principal = bootstrap.authenticator.authenticate_headers(request.headers)
        except AuthenticationError as exc:
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

    register_contract_routes(app)
    register_audit_routes(app)
    register_approval_routes(app)
    register_run_routes(app)

    # Admin and metrics routes are registered if the bootstrap provides them.
    from zeroth.service.admin_api import register_admin_routes

    register_admin_routes(app)
    register_cost_routes(app)
    register_webhook_routes(app)

    return app

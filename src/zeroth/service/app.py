"""FastAPI application factory for the deployment wrapper."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import APIRouter, FastAPI, Request
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
from zeroth.service.run_api import register_run_routes


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

        if worker is not None:
            await worker.start()
            poll_task = asyncio.create_task(worker.poll_loop(), name="worker-poll")

        # Start queue depth gauge if observability is wired.
        queue_gauge = getattr(app.state.bootstrap, "queue_gauge", None)
        if queue_gauge is not None:
            queue_gauge_task = asyncio.create_task(queue_gauge.run(), name="queue-gauge")

        yield

        # Graceful shutdown: cancel the poll loop (not the executing runs).
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        if queue_gauge_task is not None:
            queue_gauge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_gauge_task

    app = FastAPI(
        title="Zeroth Platform API",
        description="Governed medium-code platform for production-grade multi-agent systems",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.bootstrap = bootstrap

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        # Propagate or generate a correlation ID for the lifetime of this request.
        cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
        set_correlation_id(cid)
        bootstrap = app.state.bootstrap
        try:
            request.state.principal = bootstrap.authenticator.authenticate_headers(request.headers)
        except AuthenticationError as exc:
            record_service_denial(
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

    from zeroth.service.admin_api import register_admin_routes
    register_admin_routes(v1_router)

    app.include_router(v1_router)

    # Backward-compatible aliases: same routes without /v1/ prefix,
    # excluded from OpenAPI spec to avoid duplicate operationIds (per D-06, Pitfall 3)
    compat_router = APIRouter(include_in_schema=False)
    register_contract_routes(compat_router)
    register_audit_routes(compat_router)
    register_approval_routes(compat_router)
    register_run_routes(compat_router)
    register_admin_routes(compat_router)

    app.include_router(compat_router)

    return app

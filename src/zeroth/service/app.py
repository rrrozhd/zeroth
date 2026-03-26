"""FastAPI application factory for the deployment wrapper."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from zeroth.service.approval_api import register_approval_routes
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
        yield
        # Cancel in-flight background runs so shutdown does not leave stray tasks behind.
        tasks = list(app.state.run_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Zeroth Service Wrapper", lifespan=lifespan)
    app.state.bootstrap = bootstrap
    app.state.run_tasks = set()
    # Keep dispatch bounded so one busy deployment does not spawn unbounded background work.
    app.state.run_dispatch_semaphore = asyncio.Semaphore(8)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        deployment = app.state.bootstrap.deployment
        return HealthResponse(
            deployment_ref=deployment.deployment_ref,
            deployment_version=deployment.version,
            graph_version_ref=deployment.graph_version_ref,
        )

    register_contract_routes(app)
    register_approval_routes(app)
    register_run_routes(app)

    return app

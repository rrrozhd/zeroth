"""Admin control surface for run management and metrics.

Provides:
  GET  /admin/runs              -- list runs by status (admin only)
  POST /admin/runs/{id}/cancel  -- forcibly fail a run
  POST /admin/runs/{id}/replay  -- replay a dead-letter run
  POST /admin/runs/{id}/interrupt -- interrupt a running run
  GET  /metrics                 -- Prometheus-format metrics
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.runs import RunFailureState, RunStatus
from zeroth.service.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)
from zeroth.service.run_api import RunStatusResponse, _serialize_run


class AdminRunListResponse(BaseModel):
    """Response for the admin run list endpoint."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunStatusResponse]
    total: int


def register_admin_routes(app: FastAPI) -> None:
    """Register admin and metrics routes on the service app."""

    @app.get("/metrics")
    async def get_metrics(request: Request) -> Any:
        from fastapi.responses import PlainTextResponse

        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.METRICS_READ)
        await require_deployment_scope(request, bootstrap.deployment)
        metrics_collector = getattr(bootstrap, "metrics_collector", None)
        if metrics_collector is None:
            return PlainTextResponse("# no metrics collector configured\n")
        return PlainTextResponse(
            metrics_collector.render_prometheus_text(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/admin/runs", response_model=AdminRunListResponse)
    async def list_admin_runs(
        request: Request,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AdminRunListResponse:
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        deployment_ref = bootstrap.deployment.deployment_ref
        runs = await bootstrap.run_repository.list_runs(
            deployment_ref,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return AdminRunListResponse(
            runs=[_serialize_run(r) for r in runs],
            total=len(runs),
        )

    @app.post("/admin/runs/{run_id}/cancel", response_model=RunStatusResponse)
    async def cancel_run(request: Request, run_id: str) -> RunStatusResponse:
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return _serialize_run(run)
        try:
            run = await bootstrap.run_repository.transition(
                run_id,
                RunStatus.FAILED,
                failure_state=RunFailureState(
                    reason="operator_cancelled", message="cancelled by admin"
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # Clear lease so any worker won't resume it.
        lease_manager = getattr(bootstrap, "lease_manager", None)
        if lease_manager is not None:
            await lease_manager.clear_lease(run_id)
        return _serialize_run(run)

    @app.post("/admin/runs/{run_id}/replay", response_model=RunStatusResponse)
    async def replay_run(request: Request, run_id: str) -> RunStatusResponse:
        """Replay a dead-letter or failed run by resetting it to PENDING."""
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if run.status is not RunStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only failed runs can be replayed",
            )
        # Reset failure metadata.
        run.failure_state = None
        run.error = None
        run.touch()
        await bootstrap.run_repository.put(run)
        # Reset failure_count and clear lease via raw SQL.
        async with bootstrap.run_repository._store.database.transaction() as conn:
            await conn.execute(
                "UPDATE runs"
                " SET failure_count = 0, lease_worker_id = NULL, lease_expires_at = NULL"
                " WHERE run_id = ?",
                (run_id,),
            )
        try:
            run = await bootstrap.run_repository.transition(run_id, RunStatus.PENDING)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _serialize_run(run)

    @app.post("/admin/runs/{run_id}/interrupt", response_model=RunStatusResponse)
    async def interrupt_run(request: Request, run_id: str) -> RunStatusResponse:
        """Interrupt a running run (transitions to WAITING_INTERRUPT)."""
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if run.status is not RunStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only running runs can be interrupted",
            )
        try:
            run = await bootstrap.run_repository.transition(
                run_id, RunStatus.WAITING_INTERRUPT
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _serialize_run(run)


def _bootstrap(request: Request) -> Any:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap

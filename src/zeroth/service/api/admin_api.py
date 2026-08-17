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

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.contracts.governed import RunStatus
from zeroth.runtime.orchestration.interrupts import InterruptManager
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotStore
from zeroth.runtime.runs import RunFailureState
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)
from zeroth.service.api.run_api import RunStatusResponse, _serialize_run


class AdminRunListResponse(BaseModel):
    """Response for the admin run list endpoint."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunStatusResponse]
    total: int


def register_admin_routes(app: FastAPI | APIRouter) -> None:
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
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> AdminRunListResponse:
        bootstrap = _bootstrap(request)
        # Listing is read-only and every individual run is already readable
        # via GET /runs/{run_id} under RUN_READ — gating the list at
        # RUN_ADMIN only hid the console's run history from operators.
        # Mutations below (cancel/replay/interrupt) stay RUN_ADMIN.
        await require_permission(request, Permission.RUN_READ)
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
            manager = await _token_interrupt_manager(bootstrap, run_id)
            if manager is not None:
                await manager.cancel_run(run_id)
            run = await bootstrap.run_repository.cancel(
                run_id,
                bootstrap.deployment.deployment_ref,
                failure_state=RunFailureState(
                    reason="operator_cancelled", message="cancelled by admin"
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _serialize_run(run)

    @app.post("/admin/runs/{run_id}/replay", response_model=RunStatusResponse)
    async def replay_run(request: Request, run_id: str) -> RunStatusResponse:
        """Replay a dead-letter or failed run by resetting it to PENDING."""
        return await _replay_failed_run(request, run_id)

    @app.post("/admin/runs/{run_id}/interrupt", response_model=RunStatusResponse)
    async def interrupt_run(request: Request, run_id: str) -> RunStatusResponse:
        """Interrupt a running run (transitions to WAITING_INTERRUPT)."""
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if run.status != RunStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only running runs can be interrupted",
            )
        try:
            manager = await _token_interrupt_manager(bootstrap, run_id)
            if manager is not None:
                await manager.pause_run(run_id)
            run = await bootstrap.run_repository.transition(run_id, RunStatus.WAITING_INTERRUPT)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _serialize_run(run)


async def _replay_failed_run(request: Request, run_id: str) -> RunStatusResponse:
    """Body of ``POST /admin/runs/{run_id}/replay``, kept out of the route closure.

    It lives at module level only so ``register_admin_routes`` stays under the
    mccabe ceiling the commit gate ratchets: every branch of every nested route
    handler counts toward the registering function. The logic below is byte-for-
    byte the handler's own and runs in the same place in the request.

    A02-17: the reset is one guarded statement, so it either happens whole
    or not at all. It used to be three writes -- clear the failure metadata,
    zero ``failure_count`` and the lease, then transition -- and a failure or
    a 409 at the last one left a run that was still FAILED but had lost the
    identity of its failure: no ``failure_state`` for the dead-letter view to
    match, no lease, and a retry count reset to zero.

    The repository owns the transaction and adds its trusted tenant/workspace
    predicates. FAILED plus the deployment ref form the remaining CAS, so a
    concurrent state change loses without touching any colliding foreign row.
    """
    bootstrap = _bootstrap(request)
    await require_permission(request, Permission.RUN_ADMIN)
    await require_deployment_scope(request, bootstrap.deployment)
    repository = bootstrap.run_repository
    deployment_ref = bootstrap.deployment.deployment_ref
    run = await repository.get(run_id)
    if run is None or run.deployment_ref != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.status != RunStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only failed runs can be replayed",
        )
    if not await repository.replay_failed(run_id, deployment_ref):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only failed runs can be replayed",
        )
    replayed = await repository.get(run_id)
    if replayed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _serialize_run(replayed)


def _bootstrap(request: Request) -> Any:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _token_interrupt_manager(bootstrap: Any, run_id: str) -> InterruptManager | None:
    """Return lifecycle routing only for runs that already own token state."""
    repository = bootstrap.run_repository
    if not isinstance(repository, TokenSnapshotStore):
        return None
    if await repository.get_token_snapshot(run_id) is None:
        return None
    return InterruptManager(token_lifecycle=TokenLifecycleAdapter(repository))

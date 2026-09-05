"""Deployment-scoped approval query and resolution API."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.governed import RunStatus
from zeroth.governance.approvals import ApprovalDecision, ApprovalRecord, ApprovalStatus
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
    require_resource_scope,
)
from zeroth.service.api.run_api import RunStatusResponse, _serialize_run

_WORKER_WAIT_SECONDS = 5.0


class ApprovalApiBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the approval API."""

    deployment: object
    graph: object
    approval_service: object
    run_repository: object
    orchestrator: object


class ApprovalResolutionRequest(BaseModel):
    """Request body for resolving a pending approval."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    edited_payload: dict[str, Any] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class ApprovalResolutionResponse(BaseModel):
    """Response body returned after resolving an approval."""

    model_config = ConfigDict(extra="forbid")

    approval: ApprovalRecord
    run: RunStatusResponse


def register_approval_routes(app: FastAPI | APIRouter) -> None:
    """Register deployment-scoped approval query and resolution routes."""

    @app.get(
        "/deployments/{deployment_ref}/approvals",
        response_model=list[ApprovalRecord],
    )
    async def list_approvals(
        request: Request,
        deployment_ref: str,
        approval_id: str | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[ApprovalRecord]:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.APPROVAL_READ)
        if approval_id is not None:
            # The list route also supports direct lookup so clients can stay on one endpoint shape.
            record = await _require_pending_visible_approval(
                request, bootstrap, deployment, approval_id
            )
            if not _approval_matches_filters(record, run_id=run_id, thread_id=thread_id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="approval not found",
                )
            return [record]

        approvals = await bootstrap.approval_service.list_pending_visible_to_deployment(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment.deployment_ref,
            graph_version_ref=deployment.graph_version_ref,
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
        )
        return approvals

    @app.get(
        "/deployments/{deployment_ref}/approvals/{approval_id}",
        response_model=ApprovalRecord,
    )
    async def get_approval(
        request: Request,
        deployment_ref: str,
        approval_id: str,
    ) -> ApprovalRecord:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.APPROVAL_READ)
        record = await _require_visible_approval(request, bootstrap, deployment, approval_id)
        if record.status is not ApprovalStatus.PENDING:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        return record

    @app.post(
        "/deployments/{deployment_ref}/approvals/{approval_id}/resolve",
        response_model=ApprovalResolutionResponse,
    )
    async def resolve_approval(
        request: Request,
        deployment_ref: str,
        approval_id: str,
        payload: ApprovalResolutionRequest,
    ) -> ApprovalResolutionResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        principal = await require_permission(request, Permission.APPROVAL_RESOLVE)
        existing = await _require_visible_approval(request, bootstrap, deployment, approval_id)
        visible_ancestor = await bootstrap.approval_service.visible_ancestor_run(
            existing,
            deployment_ref=deployment.deployment_ref,
            graph_version_ref=deployment.graph_version_ref,
        )
        if visible_ancestor is None:  # pragma: no cover - helper above already proved it
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        is_child_approval = visible_ancestor.run_id != existing.run_id
        was_pending = existing.status is ApprovalStatus.PENDING

        try:
            resolved = await bootstrap.approval_service.resolve(
                approval_id,
                decision=payload.decision,
                actor=principal.to_actor(),
                edited_payload=payload.edited_payload,
                reason=payload.reason,
                tenant_id=existing.tenant_id,
                workspace_id=existing.workspace_id,
                deployment_ref=existing.deployment_ref,
                graph_version_ref=existing.graph_version_ref,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        run = await bootstrap.run_repository.get(resolved.run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

        if is_child_approval:
            # The child keeps its own graph/deployment identity.  The worker
            # attached to this API serves the ancestor, so notify that exact
            # ancestor instead of putting the child onto a queue nobody here
            # is authorized to claim.  Replays return the already-scheduled or
            # terminal ancestor without minting a second notification.
            current_ancestor = await bootstrap.run_repository.get(visible_ancestor.run_id)
            if current_ancestor is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
            has_worker = getattr(bootstrap, "worker", None) is not None
            try:
                if current_ancestor.status is RunStatus.WAITING_APPROVAL:
                    current_ancestor = (
                        await bootstrap.approval_service.schedule_ancestor_continuation(
                            approval_id,
                            deployment_ref=deployment.deployment_ref,
                            graph_version_ref=deployment.graph_version_ref,
                        )
                    )
                    if has_worker:
                        await _wake_worker(bootstrap, current_ancestor.run_id)
                    else:
                        current_ancestor = await bootstrap.orchestrator.resume_graph(
                            bootstrap.graph,
                            current_ancestor.run_id,
                        )
                if has_worker and current_ancestor.status in {
                    RunStatus.PENDING,
                    RunStatus.RUNNING,
                }:
                    current_ancestor = await _wait_for_worker_run(
                        bootstrap,
                        current_ancestor,
                    )
            except KeyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="run not found",
                ) from exc
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            run = current_ancestor
        elif was_pending and _run_is_waiting_for_approval(run):
            # When the durable worker is active, hand off to it via schedule_continuation,
            # then wait up to ~5 s for it to finish. That wait is BEST EFFORT: if the
            # budget expires the run is returned as-is, so the response may still carry
            # PENDING/RUNNING. Callers that need a terminal state must poll the run --
            # assuming otherwise is what made the approval API tests flaky under load
            # (ZER-21). Without a worker, fall back to the synchronous inline path.
            has_worker = getattr(bootstrap, "worker", None) is not None
            try:
                if has_worker:
                    run = await bootstrap.approval_service.schedule_continuation(approval_id)
                    await _wake_worker(bootstrap, run.run_id)
                    run = await _wait_for_worker_run(bootstrap, run)
                else:
                    run = await bootstrap.approval_service.continue_run(
                        approval_id,
                        graph=bootstrap.graph,
                        orchestrator=bootstrap.orchestrator,
                    )
            except KeyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="run not found",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        return ApprovalResolutionResponse(
            approval=resolved,
            run=_serialize_run(run),
        )


def _bootstrap(request: Request) -> ApprovalApiBootstrapLike:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _deployment_context(
    request: Request,
    deployment_ref: str,
) -> tuple[ApprovalApiBootstrapLike, object]:
    bootstrap = _bootstrap(request)
    deployment = bootstrap.deployment
    await require_deployment_scope(request, deployment)
    if getattr(deployment, "deployment_ref", None) != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    return bootstrap, deployment


async def _require_visible_approval(
    request: Request,
    bootstrap: ApprovalApiBootstrapLike,
    deployment: object,
    approval_id: str,
) -> ApprovalRecord:
    record = await bootstrap.approval_service.get_visible_to_deployment(
        approval_id,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
        deployment_ref=deployment.deployment_ref,
        graph_version_ref=deployment.graph_version_ref,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    await require_resource_scope(
        request=request,
        tenant_id=record.tenant_id,
        workspace_id=record.workspace_id,
        not_found_detail="approval not found",
    )
    return record


async def _require_pending_visible_approval(
    request: Request,
    bootstrap: ApprovalApiBootstrapLike,
    deployment: object,
    approval_id: str,
) -> ApprovalRecord:
    record = await _require_visible_approval(request, bootstrap, deployment, approval_id)
    if record.status is not ApprovalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return record


def _approval_matches_filters(
    record: ApprovalRecord,
    *,
    run_id: str | None,
    thread_id: str | None,
) -> bool:
    if run_id is not None and record.run_id != run_id:
        return False
    return thread_id is None or record.thread_id == thread_id


def _run_is_waiting_for_approval(run: object) -> bool:
    return getattr(run, "status", None) == RunStatus.WAITING_APPROVAL


async def _wake_worker(bootstrap: ApprovalApiBootstrapLike, run_id: str) -> None:
    """Best-effort ARQ wakeup; the database queue remains authoritative."""
    arq_pool = getattr(bootstrap, "arq_pool", None)
    if arq_pool is None:
        return
    from zeroth.platform.dispatch.arq_wakeup import enqueue_wakeup

    await enqueue_wakeup(arq_pool, run_id)


async def _wait_for_worker_run(bootstrap: ApprovalApiBootstrapLike, run: Any) -> Any:
    """Wait briefly for a scheduled run, returning the latest durable view."""
    import asyncio as _asyncio

    latest = run
    budget = _asyncio.timeout(_WORKER_WAIT_SECONDS)
    try:
        async with budget:
            while True:
                await _asyncio.sleep(0.05)
                current = await bootstrap.run_repository.get(run.run_id)
                if current is not None:
                    latest = current
                    if current.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                        return current
    except TimeoutError:
        if not budget.expired():
            raise
    return latest

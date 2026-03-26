"""Deployment-scoped approval query and resolution API."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.approvals import ApprovalDecision, ApprovalRecord, ApprovalStatus
from zeroth.runs import RunStatus
from zeroth.service.run_api import RunStatusResponse, _serialize_run


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
    approver: str
    edited_payload: dict[str, Any] | None = None


class ApprovalResolutionResponse(BaseModel):
    """Response body returned after resolving an approval."""

    model_config = ConfigDict(extra="forbid")

    approval: ApprovalRecord
    run: RunStatusResponse


def register_approval_routes(app: FastAPI) -> None:
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
        bootstrap, deployment = _deployment_context(request, deployment_ref)
        if approval_id is not None:
            # The list route also supports direct lookup so clients can stay on one endpoint shape.
            record = _require_pending_visible_approval(bootstrap, deployment, approval_id)
            if not _approval_matches_filters(record, run_id=run_id, thread_id=thread_id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="approval not found",
                )
            return [record]

        approvals = bootstrap.approval_service.list_pending(
            run_id=run_id,
            thread_id=thread_id,
            deployment_ref=deployment.deployment_ref,
        )
        return [
            approval
            for approval in approvals
            if _approval_visible_to_deployment(approval, deployment)
        ]

    @app.get(
        "/deployments/{deployment_ref}/approvals/{approval_id}",
        response_model=ApprovalRecord,
    )
    async def get_approval(
        request: Request,
        deployment_ref: str,
        approval_id: str,
    ) -> ApprovalRecord:
        bootstrap, deployment = _deployment_context(request, deployment_ref)
        record = _require_visible_approval(bootstrap, deployment, approval_id)
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
        bootstrap, deployment = _deployment_context(request, deployment_ref)
        existing = _require_visible_approval(bootstrap, deployment, approval_id)
        was_pending = existing.status is ApprovalStatus.PENDING

        try:
            resolved = bootstrap.approval_service.resolve(
                approval_id,
                decision=payload.decision,
                approver=payload.approver,
                edited_payload=payload.edited_payload,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        run = bootstrap.run_repository.get(resolved.run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

        if was_pending and _run_is_waiting_for_approval(run):
            # Only the first successful resolution should resume the paused workflow.
            try:
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


def _deployment_context(
    request: Request,
    deployment_ref: str,
) -> tuple[ApprovalApiBootstrapLike, object]:
    bootstrap = _bootstrap(request)
    deployment = bootstrap.deployment
    if getattr(deployment, "deployment_ref", None) != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    return bootstrap, deployment


def _require_visible_approval(
    bootstrap: ApprovalApiBootstrapLike,
    deployment: object,
    approval_id: str,
) -> ApprovalRecord:
    record = bootstrap.approval_service.get(approval_id)
    if record is None or not _approval_visible_to_deployment(record, deployment):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return record


def _require_pending_visible_approval(
    bootstrap: ApprovalApiBootstrapLike,
    deployment: object,
    approval_id: str,
) -> ApprovalRecord:
    record = _require_visible_approval(bootstrap, deployment, approval_id)
    if record.status is not ApprovalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return record


def _approval_visible_to_deployment(record: ApprovalRecord, deployment: object) -> bool:
    return (
        record.deployment_ref == getattr(deployment, "deployment_ref", None)
        and record.graph_version_ref == getattr(deployment, "graph_version_ref", None)
    )


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
    return getattr(run, "status", None) is RunStatus.WAITING_APPROVAL

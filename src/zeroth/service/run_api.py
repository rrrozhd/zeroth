"""Run invocation and status API for deployed graphs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zeroth.contracts import ContractReference
from zeroth.contracts.errors import ContractNotFoundError
from zeroth.identity import ActorIdentity
from zeroth.runs import Run, RunFailureState, RunRepository, RunStatus
from zeroth.service.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
    require_resource_scope,
)


class RunApiBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the run API."""

    deployment: object
    graph: object
    contract_registry: object
    run_repository: RunRepository
    thread_repository: object
    orchestrator: object


class RunPublicStatus(StrEnum):
    """Public run lifecycle states returned by the HTTP API."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINATED_BY_POLICY = "terminated_by_policy"
    TERMINATED_BY_LOOP_GUARD = "terminated_by_loop_guard"


class RunInvocationRequest(BaseModel):
    """Request body for creating a new run."""

    model_config = ConfigDict(extra="forbid")

    input_payload: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = None


class ApprovalPausedState(BaseModel):
    """Public state for a run paused on human approval."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str | None = None
    node_id: str
    input_payload: dict[str, Any] = Field(default_factory=dict)


class RunStatusResponse(BaseModel):
    """Public serialization of run state."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunPublicStatus
    deployment_ref: str
    graph_version_ref: str
    thread_id: str
    tenant_id: str = "default"
    workspace_id: str | None = None
    submitted_by: ActorIdentity | None = None
    current_step: str | None = None
    terminal_output: Any | None = None
    failure_state: RunFailureState | None = None
    approval_paused_state: ApprovalPausedState | None = None
    audit_refs: list[str] = Field(default_factory=list)
    timeline_ref: str | None = None
    evidence_ref: str | None = None


class RunInvocationResponse(RunStatusResponse):
    """Response body for run creation."""


def register_run_routes(app: FastAPI) -> None:
    """Register the public run API routes on the service app."""

    @app.post("/runs", response_model=RunInvocationResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: Request, payload: RunInvocationRequest) -> RunInvocationResponse:
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        graph = bootstrap.graph
        principal = require_permission(request, Permission.RUN_CREATE)
        require_deployment_scope(request, deployment, hide_as_not_found=False)
        # Validate against the pinned deployment contract version.
        validated_input = _validate_input_payload(bootstrap, payload.input_payload)
        thread_id = _validate_thread_id(bootstrap, payload.thread_id)
        run = Run(
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id=getattr(deployment, "tenant_id", "default"),
            workspace_id=getattr(deployment, "workspace_id", None),
            submitted_by=principal.to_actor(),
            thread_id=thread_id,
            current_node_ids=[],
            pending_node_ids=[_entry_step(graph)],
            metadata=_initial_metadata(graph, validated_input),
        )
        try:
            persisted = bootstrap.run_repository.create(run)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        _schedule_background_dispatch(app, bootstrap, persisted.run_id)
        return _serialize_run(bootstrap.run_repository.get(persisted.run_id) or persisted)

    @app.get("/runs/{run_id}", response_model=RunStatusResponse)
    async def get_run(request: Request, run_id: str) -> RunStatusResponse:
        bootstrap = _bootstrap(request)
        require_permission(request, Permission.RUN_READ)
        run = bootstrap.run_repository.get(run_id)
        if (
            run is None
            or run.deployment_ref != bootstrap.deployment.deployment_ref
            or run.graph_version_ref != bootstrap.deployment.graph_version_ref
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail="run not found",
        )
        return _serialize_run(run)


def _bootstrap(request: Request) -> RunApiBootstrapLike:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


def _schedule_background_dispatch(
    app: FastAPI,
    bootstrap: RunApiBootstrapLike,
    run_id: str,
) -> None:
    task = asyncio.create_task(
        _dispatch_run(
            bootstrap,
            run_id,
            semaphore=app.state.run_dispatch_semaphore,
        )
    )
    tasks = app.state.run_tasks
    # Track tasks on app state so shutdown can cancel them cleanly.
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _dispatch_run(
    bootstrap: RunApiBootstrapLike,
    run_id: str,
    *,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with semaphore:
            # The public API creates the run first, then the worker flips it into active execution.
            run = bootstrap.run_repository.transition(run_id, RunStatus.RUNNING)
            await bootstrap.orchestrator._drive(bootstrap.graph, run)
    except Exception as exc:  # pragma: no cover - defensive guard
        current = bootstrap.run_repository.get(run_id)
        if current is None:
            return
        if current.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.WAITING_APPROVAL}:
            return
        current.status = RunStatus.FAILED
        current.failure_state = RunFailureState(
            reason="dispatch_failed",
            message=str(exc),
        )
        current.touch()
        bootstrap.run_repository.put(current)


def _validate_input_payload(
    bootstrap: RunApiBootstrapLike,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    deployment = bootstrap.deployment
    contract_ref = deployment.entry_input_contract_ref
    if contract_ref is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="deployment has no entry input contract",
        )
    contract_version = deployment.entry_input_contract_version
    if contract_version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="deployment snapshot is missing pinned input contract version",
        )
    try:
        contract_model = bootstrap.contract_registry.resolve_model_type(
            ContractReference(name=contract_ref, version=contract_version)
        )
        # Model validation keeps the API contract aligned with the deployed graph snapshot.
        validated = contract_model.model_validate(payload)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"deployment input contract {contract_ref!r} version "
                f"{contract_version} not found"
            ),
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to resolve input contract {contract_ref!r}: {exc}",
        ) from exc
    return validated.model_dump(mode="json")


def _validate_thread_id(bootstrap: RunApiBootstrapLike, thread_id: str | None) -> str | None:
    """Validate that an explicit thread ID belongs to the active deployment snapshot."""
    if thread_id is None:
        return None

    thread = bootstrap.thread_repository.get(thread_id)
    if thread is None:
        # A brand-new explicit thread ID is allowed and will become the new conversation key.
        return thread_id
    deployment_tenant_id = getattr(bootstrap.deployment, "tenant_id", "default")
    deployment_workspace_id = getattr(bootstrap.deployment, "workspace_id", None)
    if (
        thread.deployment_ref != bootstrap.deployment.deployment_ref
        or thread.graph_version_ref != bootstrap.deployment.graph_version_ref
        or getattr(thread, "tenant_id", "default") != deployment_tenant_id
        or getattr(thread, "workspace_id", None) != deployment_workspace_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "thread identity mismatch: "
                f"thread_id {thread_id!r} does not belong to deployment "
                f"{bootstrap.deployment.deployment_ref!r} "
                f"at graph snapshot {bootstrap.deployment.graph_version_ref!r}"
            ),
        )
    return thread_id


def _serialize_run(run: Run) -> RunStatusResponse:
    status_value = _public_status(run)
    approval_paused_state = None
    pending_approval = _pending_approval_payload(run)
    if status_value is RunPublicStatus.PAUSED_FOR_APPROVAL and pending_approval is not None:
        # Accept a few payload shapes here so older stored runs still serialize cleanly.
        approval_paused_state = ApprovalPausedState(
            approval_id=pending_approval.get("approval_id"),
            node_id=str(
                pending_approval.get("node_id")
                or pending_approval.get("step_name")
                or run.current_step
                or (run.pending_node_ids[0] if run.pending_node_ids else "")
            ),
            input_payload=dict(
                pending_approval.get("input")
                or pending_approval.get("input_payload")
                or pending_approval.get("proposed_payload")
                or {}
            ),
        )
    return RunStatusResponse(
        run_id=run.run_id,
        status=status_value,
        deployment_ref=run.deployment_ref,
        graph_version_ref=run.graph_version_ref,
        thread_id=run.thread_id,
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        submitted_by=run.submitted_by,
        current_step=run.current_step,
        terminal_output=run.final_output,
        failure_state=run.failure_state,
        approval_paused_state=approval_paused_state,
        audit_refs=list(run.audit_refs),
        timeline_ref=f"/runs/{run.run_id}/timeline",
        evidence_ref=f"/runs/{run.run_id}/evidence",
    )


def _public_status(run: Run) -> RunPublicStatus:
    if run.status is RunStatus.PENDING:
        return RunPublicStatus.QUEUED
    if run.status is RunStatus.RUNNING:
        return RunPublicStatus.RUNNING
    if run.status is RunStatus.WAITING_APPROVAL:
        return RunPublicStatus.PAUSED_FOR_APPROVAL
    if run.status is RunStatus.COMPLETED:
        return RunPublicStatus.SUCCEEDED
    if run.status is RunStatus.FAILED:
        return _failed_status(run.failure_state)
    if run.status is RunStatus.WAITING_INTERRUPT:
        return _failed_status(run.failure_state)
    return RunPublicStatus.FAILED


def _failed_status(failure_state: RunFailureState | None) -> RunPublicStatus:
    if failure_state is None:
        return RunPublicStatus.FAILED
    if failure_state.reason == "policy_violation":
        return RunPublicStatus.TERMINATED_BY_POLICY
    if failure_state.reason.startswith("max_total_"):
        return RunPublicStatus.TERMINATED_BY_LOOP_GUARD
    return RunPublicStatus.FAILED


def _pending_approval_payload(run: Run) -> dict[str, Any] | None:
    pending = run.pending_approval or run.metadata.get("pending_approval")
    if pending is None:
        return None
    if hasattr(pending, "model_dump"):
        pending = pending.model_dump(mode="json")
    if isinstance(pending, Mapping):
        return dict(pending)
    return None


def _entry_step(graph: object) -> str:
    entry_step = getattr(graph, "entry_step", None)
    if entry_step:
        return str(entry_step)
    nodes = getattr(graph, "nodes", [])
    if not nodes:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="deployment graph has no entry step",
        )
    # Falling back to the first node preserves older graph snapshots that omitted entry_step.
    return str(nodes[0].node_id)


def _initial_metadata(graph: object, input_payload: Mapping[str, Any]) -> dict[str, Any]:
    entry_step = _entry_step(graph)
    return {
        "graph_id": getattr(graph, "graph_id", ""),
        "graph_name": getattr(graph, "name", ""),
        "node_payloads": {entry_step: dict(input_payload)},
        "edge_visit_counts": {},
        "path": [],
        "audits": {},
    }

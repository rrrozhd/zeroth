"""Run invocation and status API for deployed graphs."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph.engine_mode import token_engine_enabled
from zeroth.contracts.registry import ContractReference
from zeroth.contracts.registry.errors import ContractNotFoundError
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.guardrails.policy import (
    EffectiveGuardrailSettings,
    configured_guardrails,
)
from zeroth.governance.guardrails.rate_limit import guardrail_identity_key
from zeroth.governance.identity import ActorIdentity
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.run_repository import GuardrailAdmissionRejectedError
from zeroth.platform.primitives import utc_now
from zeroth.runtime.runs import Run, RunFailureState
from zeroth.service.api.authorization import (
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
    audit_repository: AuditRepository | None


class RunPublicStatus(StrEnum):
    """Public run lifecycle states returned by the HTTP API."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    WAITING_INTERRUPT = "waiting_interrupt"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINATED_BY_POLICY = "terminated_by_policy"
    TERMINATED_BY_LOOP_GUARD = "terminated_by_loop_guard"
    DEAD_LETTER = "dead_letter"


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


def register_run_routes(app: FastAPI | APIRouter) -> None:
    """Register the public run API routes on the service app."""

    @app.post("/runs", response_model=RunInvocationResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: Request, payload: RunInvocationRequest) -> RunInvocationResponse:
        """Validate, guard-check, and queue a new run; requires ``Permission.RUN_CREATE``."""
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        graph = bootstrap.graph
        principal = await require_permission(request, Permission.RUN_CREATE)
        await require_deployment_scope(request, deployment, hide_as_not_found=False)
        # Validate against the pinned deployment contract version.
        validated_input = await _validate_input_payload(bootstrap, payload.input_payload)
        thread_id = await _validate_thread_id(bootstrap, payload.thread_id) or ""
        run = Run(
            graph_version_ref=deployment.graph_version_ref,
            deployment_ref=deployment.deployment_ref,
            tenant_id=getattr(deployment, "tenant_id", "default"),
            workspace_id=getattr(deployment, "workspace_id", None),
            submitted_by=principal.to_actor(),
            thread_id=thread_id,
            current_node_ids=[],
            pending_node_ids=(
                [] if token_engine_enabled(graph.execution_settings) else [_entry_step(graph)]
            ),
            metadata=_initial_metadata(graph, validated_input),
        )
        try:
            persisted = await _create_guarded_run(bootstrap, run)
        except GuardrailAdmissionRejectedError as exc:
            await _record_guardrail_rejection(bootstrap, run, exc)
            raise _guardrail_http_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        # Phase 16: ARQ wakeup notification for low-latency dispatch.
        arq_pool = getattr(bootstrap, "arq_pool", None)
        if arq_pool is not None:
            from zeroth.platform.dispatch.arq_wakeup import enqueue_wakeup

            await enqueue_wakeup(arq_pool, persisted.run_id)

        # The durable worker polls for PENDING runs and dispatches them.
        return _serialize_run(await bootstrap.run_repository.get(persisted.run_id) or persisted)

    @app.get("/runs/{run_id}", response_model=RunStatusResponse)
    async def get_run(request: Request, run_id: str) -> RunStatusResponse:
        """Return the public status of a run; requires ``Permission.RUN_READ``."""
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_READ)
        run = await bootstrap.run_repository.get(run_id)
        if (
            run is None
            or run.deployment_ref != bootstrap.deployment.deployment_ref
            or run.graph_version_ref != bootstrap.deployment.graph_version_ref
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        await require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail="run not found",
        )
        return _serialize_run(run)


def _bootstrap(request: Request) -> RunApiBootstrapLike:
    """Fetch the service bootstrap from app state."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _validate_input_payload(
    bootstrap: RunApiBootstrapLike,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the input payload against the deployment's pinned entry input contract."""
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
        contract_model = await bootstrap.contract_registry.resolve_model_type(
            ContractReference(name=contract_ref, version=contract_version)
        )
        # Model validation keeps the API contract aligned with the deployed graph snapshot.
        validated = contract_model.model_validate(payload)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"deployment input contract {contract_ref!r} version {contract_version} not found"
            ),
        ) from exc
    except ValidationError as exc:
        # ctx can carry raw exception objects (e.g. the schema-contract
        # validator's ValueError) which are not JSON-serializable.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to resolve input contract {contract_ref!r}: {exc}",
        ) from exc
    return validated.model_dump(mode="json")


async def _validate_thread_id(bootstrap: RunApiBootstrapLike, thread_id: str | None) -> str | None:
    """Validate that an explicit thread ID belongs to the active deployment snapshot."""
    if thread_id is None:
        return None

    deployment_tenant_id = getattr(bootstrap.deployment, "tenant_id", "default")
    deployment_workspace_id = getattr(bootstrap.deployment, "workspace_id", None)
    thread = await bootstrap.thread_repository.get(thread_id)
    if thread is None:
        # A brand-new explicit thread ID is allowed and will become the new conversation key.
        return thread_id
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
    """Serialize a stored run into the public status response."""
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
    """Map an internal run status onto the public lifecycle state."""
    if run.status == RunStatus.PENDING:
        return RunPublicStatus.QUEUED
    if run.status == RunStatus.RUNNING:
        return RunPublicStatus.RUNNING
    if run.status == RunStatus.WAITING_APPROVAL:
        return RunPublicStatus.PAUSED_FOR_APPROVAL
    if run.status == RunStatus.COMPLETED:
        return RunPublicStatus.SUCCEEDED
    if run.status == RunStatus.FAILED:
        return _failed_status(run.failure_state)
    if run.status == RunStatus.WAITING_INTERRUPT:
        return RunPublicStatus.WAITING_INTERRUPT
    return RunPublicStatus.FAILED


def _failed_status(failure_state: RunFailureState | None) -> RunPublicStatus:
    """Refine a failed run into its public terminal state from the failure reason."""
    if failure_state is None:
        return RunPublicStatus.FAILED
    if failure_state.reason == "dead_letter":
        return RunPublicStatus.DEAD_LETTER
    if failure_state.reason == "policy_violation":
        return RunPublicStatus.TERMINATED_BY_POLICY
    if failure_state.reason.startswith("max_total_"):
        return RunPublicStatus.TERMINATED_BY_LOOP_GUARD
    return RunPublicStatus.FAILED


def _pending_approval_payload(run: Run) -> dict[str, Any] | None:
    """Extract the pending-approval payload from a run as a plain dict, if any."""
    pending = run.pending_approval or run.metadata.get("pending_approval")
    if pending is None:
        return None
    if hasattr(pending, "model_dump"):
        pending = pending.model_dump(mode="json")
    if isinstance(pending, Mapping):
        return dict(pending)
    return None


async def _check_guardrails(bootstrap: RunApiBootstrapLike, run: Run) -> None:
    """Enforce rate limits, quotas, and backpressure before accepting a run."""
    settings = await _effective_guardrail_settings(bootstrap, run.deployment_ref)
    if settings is None:
        return

    deployment_ref = run.deployment_ref
    tenant_id = run.tenant_id
    subject = None if run.submitted_by is None else run.submitted_by.subject

    # Backpressure: reject if the queue is too deep.
    backpressure_limit = settings.backpressure_queue_depth
    pending_count = await bootstrap.run_repository.count_pending(deployment_ref)
    if pending_count >= backpressure_limit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="queue capacity exceeded",
            headers={"Retry-After": "1"},
        )

    # Rate limiting.
    rate_limiter = getattr(bootstrap, "rate_limiter", None)
    if rate_limiter is not None:
        bucket_key = guardrail_identity_key(
            "token-bucket",
            tenant_id=tenant_id,
            workspace_id=run.workspace_id,
            deployment_ref=deployment_ref,
            subject=subject,
        )
        decision = await rate_limiter.decide(
            bucket_key,
            capacity=settings.bucket_capacity,
            refill_rate=settings.rate_limit_refill_rate,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    # Daily quota.
    quota_limit = settings.quota_daily_limit
    if quota_limit is not None:
        quota_enforcer = getattr(bootstrap, "quota_enforcer", None)
        if quota_enforcer is not None:
            counter_key = guardrail_identity_key(
                "daily-quota",
                tenant_id=tenant_id,
                workspace_id=run.workspace_id,
                deployment_ref=deployment_ref,
                subject=subject,
            )
            decision = await quota_enforcer.decide(
                counter_key,
                limit=quota_limit,
                window_seconds=86400,
            )
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="daily quota exceeded",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )


async def _create_guarded_run(bootstrap: RunApiBootstrapLike, run: Run) -> Run:
    settings = await _effective_guardrail_settings(bootstrap, run.deployment_ref)
    if settings is None:
        return await bootstrap.run_repository.create(run)
    rate_limiter = getattr(bootstrap, "rate_limiter", None)
    quota_enforcer = getattr(bootstrap, "quota_enforcer", None)
    if rate_limiter is None or quota_enforcer is None:
        raise HTTPException(status_code=503, detail="guardrail enforcement unavailable")
    persisted = await bootstrap.run_repository.create_guarded(
        run,
        settings=settings,
        rate_limiter=rate_limiter,
        quota_enforcer=quota_enforcer,
    )
    _record_guardrail_acceptance(bootstrap, persisted)
    return persisted


async def _effective_guardrail_settings(
    bootstrap: RunApiBootstrapLike,
    deployment_ref: str,
) -> EffectiveGuardrailSettings | None:
    """Resolve the same configured baseline and revision chain used by operators."""
    repository = getattr(bootstrap, "guardrail_policy_repository", None)
    if repository is not None:
        return await repository.effective(deployment_ref)
    config = getattr(bootstrap, "guardrail_config", None)
    if config is None:
        return None
    return configured_guardrails(config)


def _guardrail_http_error(exc: GuardrailAdmissionRejectedError) -> HTTPException:
    """Map bounded rejection reasons to their status and time-based retry hint."""
    details = {
        "queue": "queue capacity exceeded",
        "rate": "rate limit exceeded",
        "quota": "daily quota exceeded",
        "concurrency": "concurrency capacity exceeded",
    }
    return HTTPException(
        status_code=429 if exc.reason == "rate" else 503,
        detail=details.get(exc.reason, "guardrail admission rejected"),
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def _record_guardrail_acceptance(bootstrap: RunApiBootstrapLike, run: Run) -> None:
    metrics = getattr(bootstrap, "metrics_collector", None)
    if metrics is None:
        return
    metrics.increment("zeroth_guardrail_admissions_total")
    admission = run.metadata.get("guardrail_admission", {})
    if isinstance(admission, Mapping):
        for resource in ("queue", "rate", "quota"):
            value = admission.get(f"{resource}_utilization")
            if isinstance(value, int | float):
                metrics.gauge_set(
                    "zeroth_guardrail_utilization_ratio",
                    float(value),
                    labels={"resource": resource},
                )
        depth = admission.get("queue_depth")
        if isinstance(depth, int | float):
            metrics.gauge_set("zeroth_guardrail_queue_depth", float(depth))


async def _record_guardrail_rejection(
    bootstrap: RunApiBootstrapLike,
    run: Run,
    rejection: GuardrailAdmissionRejectedError,
) -> None:
    metrics = getattr(bootstrap, "metrics_collector", None)
    if metrics is not None:
        metrics.increment(
            "zeroth_guardrail_rejections_total",
            labels={"reason": rejection.reason},
        )
        metrics.gauge_set(
            "zeroth_guardrail_utilization_ratio",
            rejection.utilization,
            labels={"resource": rejection.reason},
        )
    audit_repository = bootstrap.audit_repository
    if audit_repository is None:
        return
    now = utc_now()
    await audit_repository.write(
        NodeAuditRecord(
            audit_id=f"{run.run_id}:guardrail:{rejection.reason}",
            run_id=run.run_id,
            thread_id=run.thread_id,
            node_id=f"service.guardrail.{rejection.reason}",
            graph_version_ref=run.graph_version_ref,
            deployment_ref=run.deployment_ref,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            status="rejected",
            actor=run.submitted_by,
            execution_metadata={
                "admitted": False,
                "decision": "deny",
                "enforcement_applied": True,
            },
            started_at=now,
            completed_at=now,
        )
    )


def _entry_step(graph: object) -> str:
    """Resolve the graph's entry step, falling back to its first node."""
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
    """Build the initial run metadata seeded with the entry-step payload."""
    entry_step = _entry_step(graph)
    metadata: dict[str, Any] = {
        "graph_id": getattr(graph, "graph_id", ""),
        "graph_name": getattr(graph, "name", ""),
        "edge_visit_counts": {},
        "path": [],
        "audits": {},
    }
    settings = getattr(graph, "execution_settings", None)
    if settings is not None and token_engine_enabled(settings):
        metadata.update(
            initial_input=dict(input_payload),
            node_payloads={},
            node_tags={},
        )
    else:
        metadata["node_payloads"] = {entry_step: dict(input_payload)}
    return metadata

"""Deployment-scoped public audit and timeline API."""

from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.approvals.models import ApprovalRecord
from zeroth.audit import (
    AuditQuery,
    AuditRedactionConfig,
    AuditTimelineAssembler,
    NodeAuditRecord,
    PayloadSanitizer,
    build_summary,
    collect_policy_events,
)
from zeroth.deployments.provenance import build_attestation_payload, verify_attestation
from zeroth.service.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
    require_resource_scope,
)
from zeroth.service.contracts_api import (
    DeploymentVersionMetadataResponse,
    serialize_deployment_metadata,
)
from zeroth.service.run_api import RunStatusResponse, _serialize_run

_REDACTOR = PayloadSanitizer(
    AuditRedactionConfig(redact_keys={"authorization", "api_key", "password", "secret", "token"})
)


class AuditApiBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the audit API."""

    deployment: object
    deployment_service: object
    audit_repository: object
    approval_service: object
    run_repository: object


class AuditRecordListResponse(BaseModel):
    """Public response for deployment-scoped audit lookups."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    records: list[NodeAuditRecord] = Field(default_factory=list)


class AuditTimelineResponse(BaseModel):
    """Public response for ordered audit timelines."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    run_id: str | None = None
    entries: list[NodeAuditRecord] = Field(default_factory=list)


class EvidenceSummaryResponse(BaseModel):
    """Aggregated governance counts for an evidence bundle."""

    model_config = ConfigDict(extra="forbid")

    audit_count: int = 0
    approval_count: int = 0
    tool_call_count: int = 0
    memory_interaction_count: int = 0


class RunEvidenceResponse(BaseModel):
    """Review-friendly evidence bundle for a single run."""

    model_config = ConfigDict(extra="forbid")

    run: RunStatusResponse
    audits: list[NodeAuditRecord] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    summary: EvidenceSummaryResponse
    policy_events: list[str] = Field(default_factory=list)


class DeploymentEvidenceResponse(BaseModel):
    """Review-friendly evidence bundle for a deployment snapshot."""

    model_config = ConfigDict(extra="forbid")

    deployment: DeploymentVersionMetadataResponse
    audits: list[NodeAuditRecord] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    summary: EvidenceSummaryResponse
    policy_events: list[str] = Field(default_factory=list)


class DeploymentAttestationResponse(BaseModel):
    """Stable attestation payload for a deployment snapshot."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    deployment_version: int
    graph_id: str
    graph_version: int
    graph_version_ref: str
    entry_input_contract_ref: str | None = None
    entry_input_contract_version: int | None = None
    entry_output_contract_ref: str | None = None
    entry_output_contract_version: int | None = None
    graph_snapshot_digest: str
    contract_snapshot_digest: str
    settings_snapshot_digest: str
    created_at: str
    attestation_digest: str


class AttestationVerificationResponse(BaseModel):
    """Verification result for a supplied deployment attestation."""

    model_config = ConfigDict(extra="forbid")

    verified: bool
    mismatches: list[str] = Field(default_factory=list)


def register_audit_routes(app: FastAPI) -> None:
    """Register public audit query and timeline routes."""

    @app.get(
        "/deployments/{deployment_ref}/audits",
        response_model=AuditRecordListResponse,
    )
    async def list_audits(
        request: Request,
        deployment_ref: str,
        run_id: str | None = None,
        thread_id: str | None = None,
        node_id: str | None = None,
        graph_version_ref: str | None = None,
    ) -> AuditRecordListResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.AUDIT_READ)
        records = await bootstrap.audit_repository.list(
            AuditQuery(
                run_id=run_id,
                thread_id=thread_id,
                node_id=node_id,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment.deployment_ref,
            )
        )
        return AuditRecordListResponse(
            deployment_ref=deployment.deployment_ref,
            records=[await _visible_record(request, record) for record in records],
        )

    @app.get(
        "/runs/{run_id}/timeline",
        response_model=AuditTimelineResponse,
    )
    async def get_run_timeline(
        request: Request,
        run_id: str,
    ) -> AuditTimelineResponse:
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        await require_permission(request, Permission.AUDIT_READ)
        await require_deployment_scope(request, deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is not None:
            if run.deployment_ref != deployment.deployment_ref:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
            await require_resource_scope(
                request,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                not_found_detail="run not found",
            )

        records = [
            record
            for record in await bootstrap.audit_repository.list_by_run(run_id)
            if record.deployment_ref == deployment.deployment_ref
        ]
        if run is None and not records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        visible = [
            await _visible_record(request, record, not_found_detail="run not found")
            for record in records
        ]
        timeline = AuditTimelineAssembler().assemble(visible)
        return AuditTimelineResponse(
            deployment_ref=deployment.deployment_ref,
            run_id=run_id,
            entries=list(timeline.entries),
        )

    @app.get(
        "/deployments/{deployment_ref}/timeline",
        response_model=AuditTimelineResponse,
    )
    async def get_deployment_timeline(
        request: Request,
        deployment_ref: str,
    ) -> AuditTimelineResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.AUDIT_READ)
        records = [
            await _visible_record(request, record)
            for record in await bootstrap.audit_repository.list_by_deployment(
                deployment.deployment_ref
            )
        ]
        timeline = AuditTimelineAssembler().assemble(records)
        run_ids = {record.run_id for record in timeline.entries}
        return AuditTimelineResponse(
            deployment_ref=deployment.deployment_ref,
            run_id=next(iter(run_ids)) if len(run_ids) == 1 else None,
            entries=list(timeline.entries),
        )

    @app.get(
        "/runs/{run_id}/evidence",
        response_model=RunEvidenceResponse,
    )
    async def get_run_evidence(
        request: Request,
        run_id: str,
    ) -> RunEvidenceResponse:
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        await require_permission(request, Permission.AUDIT_READ)
        await require_deployment_scope(request, deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        await require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail="run not found",
        )
        audits = [
            await _visible_record(request, record, not_found_detail="run not found")
            for record in await bootstrap.audit_repository.list_by_run(run_id)
            if record.deployment_ref == deployment.deployment_ref
        ]
        approvals = await _visible_approvals(
            request,
            await bootstrap.approval_service.list(run_id=run_id),
            not_found_detail="run not found",
        )
        return RunEvidenceResponse(
            run=_serialize_run(run),
            audits=audits,
            approvals=approvals,
            summary=EvidenceSummaryResponse.model_validate(build_summary(audits, approvals)),
            policy_events=collect_policy_events(audits),
        )

    @app.get(
        "/deployments/{deployment_ref}/evidence",
        response_model=DeploymentEvidenceResponse,
    )
    async def get_deployment_evidence(
        request: Request,
        deployment_ref: str,
    ) -> DeploymentEvidenceResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.AUDIT_READ)
        audits = [
            await _visible_record(request, record)
            for record in await bootstrap.audit_repository.list_by_deployment(
                deployment.deployment_ref
            )
        ]
        approvals = await _visible_approvals(
            request,
            await bootstrap.approval_service.list(deployment_ref=deployment.deployment_ref),
        )
        run_ids = sorted(
            {record.run_id for record in audits} | {record.run_id for record in approvals}
        )
        return DeploymentEvidenceResponse(
            deployment=serialize_deployment_metadata(deployment),
            audits=audits,
            approvals=approvals,
            run_ids=run_ids,
            summary=EvidenceSummaryResponse.model_validate(build_summary(audits, approvals)),
            policy_events=collect_policy_events(audits),
        )

    @app.get(
        "/deployments/{deployment_ref}/attestation",
        response_model=DeploymentAttestationResponse,
    )
    async def get_attestation(
        request: Request,
        deployment_ref: str,
    ) -> DeploymentAttestationResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        current = _load_bound_deployment(bootstrap)
        return DeploymentAttestationResponse.model_validate(build_attestation_payload(current))

    @app.post(
        "/deployments/{deployment_ref}/verify-attestation",
        response_model=AttestationVerificationResponse,
    )
    async def post_verify_attestation(
        request: Request,
        deployment_ref: str,
        attestation: DeploymentAttestationResponse,
    ) -> AttestationVerificationResponse:
        bootstrap, _ = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        current = _load_bound_deployment(bootstrap)
        mismatches = verify_attestation(current, attestation.model_dump(mode="json"))
        return AttestationVerificationResponse(
            verified=not mismatches,
            mismatches=mismatches,
        )


def _bootstrap(request: Request) -> AuditApiBootstrapLike:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _deployment_context(
    request: Request,
    deployment_ref: str,
) -> tuple[AuditApiBootstrapLike, object]:
    bootstrap = _bootstrap(request)
    deployment = bootstrap.deployment
    await require_deployment_scope(request, deployment)
    if getattr(deployment, "deployment_ref", None) != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    return bootstrap, deployment


async def _visible_record(
    request: Request,
    record: NodeAuditRecord,
    *,
    not_found_detail: str = "audit not found",
) -> NodeAuditRecord:
    await require_resource_scope(
        request,
        tenant_id=record.tenant_id,
        workspace_id=record.workspace_id,
        not_found_detail=not_found_detail,
    )
    return record.model_copy(
        update={
            "input_snapshot": _sanitize_mapping(record.input_snapshot),
            "output_snapshot": _sanitize_mapping(record.output_snapshot),
            "execution_metadata": _sanitize_mapping(record.execution_metadata),
        }
    )


def _sanitize_mapping(payload: dict[str, object]) -> dict[str, object]:
    return dict(_REDACTOR.sanitize(payload))


async def _visible_approvals(
    request: Request,
    approvals: list[ApprovalRecord],
    *,
    not_found_detail: str = "approval not found",
) -> list[ApprovalRecord]:
    visible: list[ApprovalRecord] = []
    for approval in approvals:
        await require_resource_scope(
            request,
            tenant_id=approval.tenant_id,
            workspace_id=approval.workspace_id,
            not_found_detail=not_found_detail,
        )
        visible.append(approval)
    return visible


def _load_bound_deployment(bootstrap: AuditApiBootstrapLike) -> object:
    deployment = bootstrap.deployment_service.get(
        bootstrap.deployment.deployment_ref,
        bootstrap.deployment.version,
    )
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    return deployment

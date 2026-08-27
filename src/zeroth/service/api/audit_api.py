"""Deployment-scoped public audit and timeline API."""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.governance.approvals.models import ApprovalRecord
from zeroth.governance.audit import (
    AuditContinuityVerifier,
    AuditQuery,
    AuditRedactionConfig,
    AuditRepository,
    AuditTimelineAssembler,
    NodeAuditRecord,
    PayloadSanitizer,
    build_summary,
    collect_policy_events,
)
from zeroth.governance.audit.readiness import signed_audit_required, signer_is_available
from zeroth.runtime.runs import Run
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
    require_resource_scope,
)
from zeroth.service.api.contracts_api import (
    DeploymentVersionMetadataResponse,
    serialize_deployment_metadata,
)
from zeroth.service.api.delivery_types import AuditReadinessResponse
from zeroth.service.api.deployment_context import require_scoped_deployment
from zeroth.service.api.run_api import RunStatusResponse, _serialize_run
from zeroth.service.deployments.provenance import (
    build_attestation_payload,
    verify_attestation_full,
)

_REDACTOR = PayloadSanitizer(
    AuditRedactionConfig(redact_keys={"authorization", "api_key", "password", "secret", "token"})
)
_MAX_COMPOSED_EVIDENCE_RUNS = 1000


class AuditApiBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the audit API."""

    deployment: object
    deployment_service: object
    audit_repository: AuditRepository
    approval_service: object
    run_repository: object
    # WS-D: process-wide provenance signer (may be None -> unsigned-legacy).
    signer: object


class AuditRecordListResponse(BaseModel):
    """Public response for deployment-scoped audit lookups."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    records: list[NodeAuditRecord] = Field(default_factory=list)


class TenantAuditRecordListResponse(BaseModel):
    """Tenant-scoped audit records across all deployments visible to the caller."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    records: list[NodeAuditRecord] = Field(default_factory=list)


class AuditVerificationResponse(BaseModel):
    """Result of verifying the audit chain for a scope.

    ``verified`` is the unkeyed digest-continuity axis. ``signature_verified`` is
    the independent WS-D keyed axis, three-state: True (all signed records
    valid), False (a signed record failed), null (unsigned-legacy — render
    neutral, never green or red).
    """

    model_config = ConfigDict(extra="forbid")

    scope: str
    verified: bool
    record_count: int = 0
    failed_audit_id: str | None = None
    error: str | None = None
    signature_verified: bool | None = None
    signing_key_id: str | None = None
    unsigned_record_count: int = 0


class VerifyChainRequest(BaseModel):
    """Optional body for POST /runs/{run_id}/verify-chain.

    A client that recorded the chain head out-of-band can pin it: verification
    additionally fails if the persisted head digest does not match.
    """

    model_config = ConfigDict(extra="forbid")

    expected_head_digest: str | None = None


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
    priced_call_count: int = 0
    cost_event_count: int = 0
    total_cost_usd: float = 0.0
    cost_identity_state: str = "not_applicable_no_priced_call"
    reconciliation_state: str = "reconciled_zero_activity"


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
    attestation_payload_version: int = Field(default=1, ge=1)
    engine_mode: str = "legacy"
    entry_input_contract_ref: str | None = None
    entry_input_contract_version: int | None = None
    entry_output_contract_ref: str | None = None
    entry_output_contract_version: int | None = None
    graph_snapshot_digest: str
    contract_snapshot_digest: str
    settings_snapshot_digest: str
    created_at: str
    attestation_digest: str
    # WS-D keyed signature over ``attestation_digest`` (null -> unsigned-legacy).
    attestation_signature: str | None = None
    attestation_signing_key_id: str | None = None
    attestation_algorithm: str | None = None


class AttestationVerificationResponse(BaseModel):
    """Dual-check verification result for a deployment attestation.

    ``verified`` stays the overall pass/fail. ``digest_verified`` is the digest
    recompute axis; ``signature_verified`` is the keyed axis (three-state, null =
    unsigned-legacy). Neither masks the other.
    """

    model_config = ConfigDict(extra="forbid")

    verified: bool
    mismatches: list[str] = Field(default_factory=list)
    digest_verified: bool = False
    signature_verified: bool | None = None
    signing_key_id: str | None = None


def register_audit_routes(app: FastAPI | APIRouter) -> None:
    """Register public audit query and timeline routes."""

    @app.get("/audit-readiness", response_model=AuditReadinessResponse)
    async def audit_readiness(request: Request) -> AuditReadinessResponse:
        await require_permission(request, Permission.AUDIT_READ)
        bootstrap = request.app.state.bootstrap
        graph = bootstrap.graph
        deployment_mode = getattr(bootstrap.deployment_service, "deployment_mode", "local")
        consequential = signed_audit_required(graph, "local")
        required = signed_audit_required(graph, deployment_mode)
        available = signer_is_available(getattr(bootstrap, "signer", None))
        ready = available or not required
        if available:
            state = "signed"
            message = "Audit records and deployment attestations are signed."
        elif required:
            state = "blocked_unsigned"
            message = "Signing is required for this deployment; configure provenance key material."
        else:
            state = "local_unsigned"
            message = (
                "LOCAL ONLY — audit digests are unsigned and do not establish keyed provenance."
            )
        return AuditReadinessResponse(
            ready=ready,
            state=state,
            deployment_mode=deployment_mode,
            signing_required=required,
            signer_available=available,
            consequential_actions=consequential,
            message=message,
        )

    @app.get(
        "/admin/audits",
        response_model=TenantAuditRecordListResponse,
    )
    async def list_tenant_audits(request: Request) -> TenantAuditRecordListResponse:
        """List audit records across the caller's tenant-visible deployments."""
        bootstrap = _bootstrap(request)
        principal = await require_permission(request, Permission.AUDIT_READ)
        records = await bootstrap.audit_repository.list(
            AuditQuery(
                tenant_id=principal.tenant_id,
                workspace_id=principal.workspace_id,
                workspace_scoped=principal.workspace_id is not None,
            )
        )
        return TenantAuditRecordListResponse(
            scope=f"tenant:{principal.tenant_id}",
            records=[await _visible_record(request, record) for record in records],
        )

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
        """List a deployment's audit records; requires ``Permission.AUDIT_READ``."""
        bootstrap, deployment, principal = await require_scoped_deployment(
            request, deployment_ref, Permission.AUDIT_READ
        )
        records = await bootstrap.audit_repository.list(
            AuditQuery(
                run_id=run_id,
                thread_id=thread_id,
                node_id=node_id,
                graph_version_ref=graph_version_ref,
                deployment_ref=deployment.deployment_ref,
                tenant_id=principal.tenant_id,  # WS-B: tenant-scoped audit query
                workspace_id=deployment.workspace_id,
                workspace_scoped=True,
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
        """Return the ordered audit timeline for a run; requires ``Permission.AUDIT_READ``."""
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        principal = await require_permission(request, Permission.AUDIT_READ)
        await require_deployment_scope(request, deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is not None:
            await require_resource_scope(
                request,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                not_found_detail="run not found",
            )

        target_deployment_ref = run.deployment_ref if run is not None else deployment.deployment_ref
        target_workspace_id = run.workspace_id if run is not None else deployment.workspace_id
        records = await bootstrap.audit_repository.list_by_run(
            run_id,
            tenant_id=principal.tenant_id,
            workspace_id=target_workspace_id,
            workspace_scoped=True,
        )
        if run is None and not records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        visible = [
            await _visible_record(request, record, not_found_detail="run not found")
            for record in records
        ]
        timeline = AuditTimelineAssembler().assemble(visible)
        return AuditTimelineResponse(
            deployment_ref=target_deployment_ref,
            run_id=run_id,
            entries=list(timeline.entries),
        )

    @app.get(
        "/runs/{run_id}/audit-verification",
        response_model=AuditVerificationResponse,
    )
    async def verify_run_audit_chain(
        request: Request,
        run_id: str,
    ) -> AuditVerificationResponse:
        """Verify the digest chain + signatures over a run's audit records."""
        return await _verify_run_chain(request, run_id)

    @app.post(
        "/runs/{run_id}/verify-chain",
        response_model=AuditVerificationResponse,
    )
    async def post_verify_run_chain(
        request: Request,
        run_id: str,
        body: VerifyChainRequest | None = None,
    ) -> AuditVerificationResponse:
        """POST alias of the run audit-verification with an optional head pin."""
        expected_head = body.expected_head_digest if body is not None else None
        return await _verify_run_chain(request, run_id, expected_head_digest=expected_head)

    async def _verify_run_chain(
        request: Request,
        run_id: str,
        *,
        expected_head_digest: str | None = None,
    ) -> AuditVerificationResponse:
        """Shared chain verification behind the GET/POST run verification routes."""
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        principal = await require_permission(request, Permission.AUDIT_READ)
        await require_deployment_scope(request, deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        await require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail="run not found",
        )

        signer = getattr(bootstrap, "signer", None)
        verifier = _verification_provider(bootstrap)
        report = await AuditContinuityVerifier(
            bootstrap.audit_repository, signer=verifier
        ).verify_run(
            run_id,
            tenant_id=principal.tenant_id,
            workspace_id=run.workspace_id,
            workspace_scoped=True,
        )
        response = _verification_response(report, signer)
        # Optional client-pinned head: fail verification if the persisted chain
        # head digest differs from what the caller recorded out-of-band.
        if expected_head_digest is not None and response.verified:
            records = await bootstrap.audit_repository.list_by_run(
                run_id,
                tenant_id=principal.tenant_id,
                workspace_id=run.workspace_id,
                workspace_scoped=True,
            )
            head_digest = records[-1].record_digest if records else None
            if head_digest != expected_head_digest:
                response = response.model_copy(
                    update={
                        "verified": False,
                        "error": "expected head digest mismatch",
                        "failed_audit_id": records[-1].audit_id if records else None,
                    }
                )
        return response

    @app.get(
        "/deployments/{deployment_ref}/audit-verification",
        response_model=AuditVerificationResponse,
    )
    async def verify_deployment_audit_chain(
        request: Request,
        deployment_ref: str,
    ) -> AuditVerificationResponse:
        """Verify digest continuity + signatures across every run of a deployment."""
        bootstrap, deployment, principal = await require_scoped_deployment(
            request, deployment_ref, Permission.AUDIT_READ
        )
        signer = getattr(bootstrap, "signer", None)
        verifier = _verification_provider(bootstrap)
        report = await AuditContinuityVerifier(
            bootstrap.audit_repository, signer=verifier
        ).verify_deployment(
            deployment.deployment_ref,
            tenant_id=principal.tenant_id,
            workspace_id=deployment.workspace_id,
            workspace_scoped=True,
        )
        return _verification_response(report, signer)

    @app.get(
        "/deployments/{deployment_ref}/timeline",
        response_model=AuditTimelineResponse,
    )
    async def get_deployment_timeline(
        request: Request,
        deployment_ref: str,
    ) -> AuditTimelineResponse:
        """Return a deployment's ordered audit timeline; requires ``Permission.AUDIT_READ``."""
        bootstrap, deployment, principal = await require_scoped_deployment(
            request, deployment_ref, Permission.AUDIT_READ
        )
        records = [
            await _visible_record(request, record)
            for record in await bootstrap.audit_repository.list_by_deployment(
                deployment.deployment_ref,
                tenant_id=principal.tenant_id,
                workspace_id=deployment.workspace_id,
                workspace_scoped=True,
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
        """Return the evidence bundle for a run; requires ``Permission.AUDIT_READ``."""
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        principal = await require_permission(request, Permission.AUDIT_READ)
        await require_deployment_scope(request, deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        await require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail="run not found",
        )
        evidence_runs = await _composed_evidence_runs(request, bootstrap, run)
        audits = await _composed_audits(
            request,
            bootstrap,
            evidence_runs,
            tenant_id=principal.tenant_id,
        )
        approvals = await _composed_approvals(
            request,
            bootstrap,
            evidence_runs,
            tenant_id=principal.tenant_id,
        )
        summary = build_summary(audits, approvals)
        if len(evidence_runs) > 1:
            cost_summary = build_summary(
                [
                    record
                    for record in audits
                    if not _is_parent_branch_cost_rollup(record, root_run_id=run.run_id)
                ],
                approvals,
            )
            for field in (
                "priced_call_count",
                "cost_event_count",
                "total_cost_usd",
                "cost_identity_state",
                "reconciliation_state",
            ):
                summary[field] = cost_summary[field]
        return RunEvidenceResponse(
            run=_serialize_run(run),
            audits=audits,
            approvals=approvals,
            summary=EvidenceSummaryResponse.model_validate(summary),
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
        """Return the evidence bundle for a deployment; requires ``Permission.AUDIT_READ``."""
        bootstrap, deployment, principal = await require_scoped_deployment(
            request, deployment_ref, Permission.AUDIT_READ
        )
        audits = [
            await _visible_record(request, record)
            for record in await bootstrap.audit_repository.list_by_deployment(
                deployment.deployment_ref,
                tenant_id=principal.tenant_id,
                workspace_id=deployment.workspace_id,
                workspace_scoped=True,
            )
        ]
        approvals = await _visible_approvals(
            request,
            await bootstrap.approval_service.list_visible_to_deployment(
                deployment_ref=deployment.deployment_ref,
                graph_version_ref=deployment.graph_version_ref,
                tenant_id=principal.tenant_id,
                workspace_id=deployment.workspace_id,
            ),
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
        """Return the persisted deployment attestation; requires ``Permission.DEPLOYMENT_READ``."""
        bootstrap, deployment, _ = await require_scoped_deployment(
            request, deployment_ref, Permission.DEPLOYMENT_READ
        )
        # Return the PERSISTED signature (do not re-sign an unsigned payload):
        # the attestation must reflect what was signed at deploy time.
        payload = build_attestation_payload(deployment)
        payload["attestation_signature"] = getattr(deployment, "attestation_signature", None)
        payload["attestation_signing_key_id"] = getattr(
            deployment, "attestation_signing_key_id", None
        )
        payload["attestation_algorithm"] = getattr(deployment, "attestation_algorithm", None)
        return DeploymentAttestationResponse.model_validate(payload)

    @app.post(
        "/deployments/{deployment_ref}/verify-attestation",
        response_model=AttestationVerificationResponse,
    )
    async def post_verify_attestation(
        request: Request,
        deployment_ref: str,
        attestation: DeploymentAttestationResponse,
    ) -> AttestationVerificationResponse:
        """Verify a client-supplied attestation against the bound deployment."""
        bootstrap, deployment, _ = await require_scoped_deployment(
            request, deployment_ref, Permission.DEPLOYMENT_READ
        )
        verifier = _verification_provider(bootstrap)
        mismatches, signature_ok = verify_attestation_full(
            deployment, attestation.model_dump(mode="json"), verifier
        )
        return _attestation_verification_response(deployment, mismatches, signature_ok)

    @app.get(
        "/deployments/{deployment_ref}/attestation/verify",
        response_model=AttestationVerificationResponse,
    )
    async def get_attestation_verify(
        request: Request,
        deployment_ref: str,
    ) -> AttestationVerificationResponse:
        """Server self-verifies its persisted attestation (digest + signature)."""
        bootstrap, deployment, _ = await require_scoped_deployment(
            request, deployment_ref, Permission.DEPLOYMENT_READ
        )
        verifier = _verification_provider(bootstrap)
        mismatches, signature_ok = verify_attestation_full(
            deployment, build_attestation_payload(deployment), verifier
        )
        return _attestation_verification_response(deployment, mismatches, signature_ok)


def _verification_provider(bootstrap: object) -> object | None:
    """Return the keyring used to verify historical provenance signatures.

    Signing uses only the active key. Verification must retain rotated keys so
    an otherwise valid historical chain does not become broken after rotation.
    Older bootstrap surfaces without a dedicated verifier remain compatible.
    """
    return getattr(bootstrap, "verifier", None) or getattr(bootstrap, "signer", None)


def _signer_key_id(signer: object | None) -> str | None:
    """Return the active signer's key id, or None when there is no signer."""
    key_id = getattr(signer, "key_id", None)
    return key_id() if callable(key_id) else None


def _verification_response(report: object, signer: object | None) -> AuditVerificationResponse:
    """Map a continuity report + active signer onto the public response.

    ``signing_key_id`` reflects the active signer only when the scope has signed
    records (``signature_verified`` is not None); it is a display aid, not a
    per-record attribution.
    """
    signing_key_id = None
    if getattr(report, "signature_verified", None) is not None:
        signing_key_id = _signer_key_id(signer)
    return AuditVerificationResponse(
        scope=report.scope,
        verified=report.verified,
        record_count=report.record_count,
        failed_audit_id=report.failed_audit_id,
        error=report.error,
        signature_verified=report.signature_verified,
        signing_key_id=signing_key_id,
        unsigned_record_count=getattr(report, "unsigned_record_count", 0),
    )


def _attestation_verification_response(
    deployment: object,
    mismatches: list[str],
    signature_ok: bool | None,
) -> AttestationVerificationResponse:
    """Combine the two independent axes into the dual-check response.

    Overall ``verified`` requires the digest recompute to pass AND the signature
    not to be a definite failure. Unsigned-legacy (``signature_ok is None``)
    leaves the digest axis in charge, preserving pre-WS-D behavior.
    """
    digest_verified = not mismatches
    verified = digest_verified and signature_ok is not False
    return AttestationVerificationResponse(
        verified=verified,
        mismatches=mismatches,
        digest_verified=digest_verified,
        signature_verified=signature_ok,
        signing_key_id=getattr(deployment, "attestation_signing_key_id", None),
    )


def _bootstrap(request: Request) -> AuditApiBootstrapLike:
    """Fetch the service bootstrap from app state."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _composed_evidence_runs(
    request: Request,
    bootstrap: AuditApiBootstrapLike,
    root: Run,
) -> list[Run]:
    """Return one bounded parent-first run lineage without crossing root scope.

    Every run retains its own audit chain.  This helper only determines which
    independently signed chains belong in the composed evidence view; it never
    copies, re-signs, or rewrites an audit record.
    """
    root_run_id = root.run_id
    root_tenant_id = root.tenant_id
    root_workspace_id = root.workspace_id
    ordered = [root]
    pending = [root]
    seen = {root_run_id}
    while pending:
        parent = pending.pop(0)
        children = sorted(
            await bootstrap.run_repository.list_child_runs(parent.run_id),
            key=lambda child: child.run_id,
        )
        for child in children:
            if (
                child.parent_run_id != parent.run_id
                or child.tenant_id != root_tenant_id
                or child.workspace_id != root_workspace_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="run not found",
                )
            await require_resource_scope(
                request,
                tenant_id=child.tenant_id,
                workspace_id=child.workspace_id,
                not_found_detail="run not found",
            )
            if child.run_id in seen:
                continue
            if len(seen) >= _MAX_COMPOSED_EVIDENCE_RUNS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="composed run evidence exceeds the supported lineage bound",
                )
            seen.add(child.run_id)
            ordered.append(child)
            pending.append(child)
    return ordered


async def _composed_audits(
    request: Request,
    bootstrap: AuditApiBootstrapLike,
    runs: list[Run],
    *,
    tenant_id: str,
) -> list[NodeAuditRecord]:
    """Flatten independent run chains once each, preserving signed rows verbatim."""
    visible: list[NodeAuditRecord] = []
    seen: set[str] = set()
    for run in runs:
        records = await bootstrap.audit_repository.list_by_run(
            run.run_id,
            tenant_id=tenant_id,
            workspace_id=run.workspace_id,
            workspace_scoped=True,
        )
        for record in records:
            if record.audit_id in seen:
                continue
            seen.add(record.audit_id)
            visible.append(
                await _visible_record(request, record, not_found_detail="run not found")
            )
    return visible


async def _composed_approvals(
    request: Request,
    bootstrap: AuditApiBootstrapLike,
    runs: list[Run],
    *,
    tenant_id: str,
) -> list[ApprovalRecord]:
    """Include child-owned approvals once, under each child's immutable identity."""
    visible: list[ApprovalRecord] = []
    seen: set[str] = set()
    for run in runs:
        approvals = await _visible_approvals(
            request,
            await bootstrap.approval_service.list_visible_to_deployment(
                run_id=run.run_id,
                tenant_id=tenant_id,
                workspace_id=run.workspace_id,
                deployment_ref=run.deployment_ref,
                graph_version_ref=run.graph_version_ref,
            ),
            not_found_detail="run not found",
        )
        for approval in approvals:
            if approval.approval_id in seen:
                continue
            seen.add(approval.approval_id)
            visible.append(approval)
    return visible


def _is_parent_branch_cost_rollup(record: NodeAuditRecord, *, root_run_id: str) -> bool:
    """Identify a composed parent's timeline-only copy of child cost.

    The child audit chain owns provider identity. A parent branch row intentionally
    repeats the amount for traversal display, but has no token usage or cost-event
    identity and therefore is not an additional priced call.
    """
    return (
        record.run_id == root_run_id
        and record.cost_event_id is None
        and record.token_usage is None
        and record.execution_metadata.get("branch_id") is not None
        and (
            float(record.cost_usd or 0.0) > 0.0
            or float(record.estimated_cost_usd or 0.0) > 0.0
        )
    )


async def _visible_record(
    request: Request,
    record: NodeAuditRecord,
    *,
    not_found_detail: str = "audit not found",
) -> NodeAuditRecord:
    """Scope-check an audit record and return it with sensitive keys redacted."""
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
    """Redact sensitive keys from an audit payload mapping."""
    return dict(_REDACTOR.sanitize(payload))


async def _visible_approvals(
    request: Request,
    approvals: list[ApprovalRecord],
    *,
    not_found_detail: str = "approval not found",
) -> list[ApprovalRecord]:
    """Enforce resource scope on each approval and return the scoped list."""
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

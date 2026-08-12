"""WS-E retention, legal-hold, and right-to-erasure API.

All routes require the :data:`Permission.RETENTION_ADMIN` permission (admin-tier)
and are scoped to the caller's tenant via ``require_resource_scope`` — a principal
can only see and act on its own tenant's policy, holds, and runs.

Right-to-erasure granularity is run_id or whole-tenant. There is intentionally no
subject -> record index (a run is the finest unit that maps to a data subject
here); see docs/retention-and-erasure.md. An active legal hold covering the target
makes an erasure request fail with 409 Conflict — a hold beats erasure.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.governance.audit import AuditQuery
from zeroth.governance.identity import AuthenticatedPrincipal
from zeroth.governance.retention.erasure_service import LegalHoldError
from zeroth.governance.retention.models import LegalHold, RetentionPolicy
from zeroth.service.api.authorization import (
    Permission,
    require_permission,
    require_resource_scope,
)


class RetentionBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the retention API."""

    audit_repository: object
    run_repository: object
    retention_policy_repository: object
    legal_hold_repository: object
    retention_erasure_service: object


class RetentionPolicyBody(BaseModel):
    """Request body for PUT /retention/policy."""

    model_config = ConfigDict(extra="forbid")

    audit_ttl_seconds: int | None = Field(default=None, ge=1)
    run_ttl_seconds: int | None = Field(default=None, ge=1)
    enabled: bool = True


class RetentionPolicyResponse(BaseModel):
    """A tenant's resolved retention policy."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    audit_ttl_seconds: int | None = Field(default=None, ge=1)
    run_ttl_seconds: int | None = Field(default=None, ge=1)
    enabled: bool = True


class LegalHoldBody(BaseModel):
    """Request body for POST /retention/legal-holds.

    ``run_id`` omitted places a tenant-wide hold (freezes every run).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    reason: str | None = None


class LegalHoldResponse(BaseModel):
    """A placed or listed legal hold."""

    model_config = ConfigDict(extra="forbid")

    hold_id: str
    tenant_id: str
    run_id: str | None = None
    reason: str | None = None
    active: bool = True
    placed_by: str | None = None


class ErasureRequestBody(BaseModel):
    """Request body for POST /retention/erasure-requests.

    Exactly one of ``run_id`` (erase one run) or ``tenant_id`` (erase every
    non-held run for the tenant) must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    tenant_id: str | None = None


class ErasureRunResult(BaseModel):
    """Per-run erasure counts."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    audits_erased: int = 0
    checkpoints_deleted: int = 0
    run_redacted: bool = False
    artifacts_deleted: int = 0
    econ_events_deleted: int | None = None


class ErasureResponse(BaseModel):
    """Result of an erasure request (one or many runs)."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    runs: list[ErasureRunResult] = Field(default_factory=list)


def _policy_response(policy: RetentionPolicy) -> RetentionPolicyResponse:
    """Map a domain retention policy onto the public response model."""
    return RetentionPolicyResponse(
        tenant_id=policy.tenant_id,
        audit_ttl_seconds=policy.audit_ttl_seconds,
        run_ttl_seconds=policy.run_ttl_seconds,
        enabled=policy.enabled,
    )


def _hold_response(hold: LegalHold) -> LegalHoldResponse:
    """Map a domain legal hold onto the public response model."""
    return LegalHoldResponse(
        hold_id=hold.hold_id,
        tenant_id=hold.tenant_id,
        run_id=hold.run_id,
        reason=hold.reason,
        active=hold.active,
        placed_by=hold.placed_by,
    )


def _bootstrap(request: Request) -> RetentionBootstrapLike:
    """Fetch the service bootstrap from app state."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


def register_retention_routes(app: FastAPI | APIRouter) -> None:
    """Register retention policy, legal-hold, and erasure routes."""

    @app.get("/retention/policy", response_model=RetentionPolicyResponse)
    async def get_retention_policy(request: Request) -> RetentionPolicyResponse:
        """Return the caller tenant's resolved retention policy (admin-tier)."""
        principal = await require_permission(request, Permission.RETENTION_ADMIN)
        bootstrap = _bootstrap(request)
        if principal.tenant_id != bootstrap.retention_policy_repository.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="policy not found")
        policy = await bootstrap.retention_policy_repository.resolve()
        return _policy_response(policy)

    @app.put("/retention/policy", response_model=RetentionPolicyResponse)
    async def put_retention_policy(
        request: Request,
        body: RetentionPolicyBody,
    ) -> RetentionPolicyResponse:
        """Create or replace the caller tenant's retention policy (admin-tier)."""
        principal = await require_permission(request, Permission.RETENTION_ADMIN)
        bootstrap = _bootstrap(request)
        policy = RetentionPolicy(
            tenant_id=principal.tenant_id,
            audit_ttl_seconds=body.audit_ttl_seconds,
            run_ttl_seconds=body.run_ttl_seconds,
            enabled=body.enabled,
        )
        saved = await bootstrap.retention_policy_repository.upsert(policy)
        return _policy_response(saved)

    @app.post(
        "/retention/legal-holds",
        response_model=LegalHoldResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def place_legal_hold(
        request: Request,
        body: LegalHoldBody,
    ) -> LegalHoldResponse:
        """Place a run-scoped or tenant-wide legal hold for the caller's tenant."""
        principal = await require_permission(request, Permission.RETENTION_ADMIN)
        bootstrap = _bootstrap(request)
        # A run-scoped hold must target a run the caller's tenant owns.
        if body.run_id is not None:
            await _require_run_tenant(request, bootstrap, body.run_id)
        hold = await bootstrap.legal_hold_repository.place(
            principal.tenant_id,
            run_id=body.run_id,
            reason=body.reason,
            placed_by=principal.subject,
        )
        return _hold_response(hold)

    @app.delete("/retention/legal-holds/{hold_id}", response_model=LegalHoldResponse)
    async def release_legal_hold(
        request: Request,
        hold_id: str,
    ) -> LegalHoldResponse:
        """Release a legal hold owned by the caller's tenant (404 otherwise)."""
        await require_permission(request, Permission.RETENTION_ADMIN)
        bootstrap = _bootstrap(request)
        hold = await bootstrap.legal_hold_repository.get(hold_id)
        if hold is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="legal hold not found"
            )
        # Cross-tenant deny: a principal may only release its own tenant's holds.
        await require_resource_scope(
            request,
            tenant_id=hold.tenant_id,
            workspace_id=None,
            not_found_detail="legal hold not found",
        )
        await bootstrap.legal_hold_repository.release(hold_id)
        released = await bootstrap.legal_hold_repository.get(hold_id)
        return _hold_response(released or hold)

    @app.post("/retention/erasure-requests", response_model=ErasureResponse)
    async def request_erasure(
        request: Request,
        body: ErasureRequestBody,
    ) -> ErasureResponse:
        """Erase one run or every non-held run of the caller's tenant (409 on hold)."""
        principal = await require_permission(request, Permission.RETENTION_ADMIN)
        bootstrap = _bootstrap(request)
        if (body.run_id is None) == (body.tenant_id is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="exactly one of run_id or tenant_id is required",
            )

        if body.tenant_id is not None:
            # Tenant-wide erasure is confined to the caller's own tenant.
            await require_resource_scope(
                request,
                tenant_id=body.tenant_id,
                workspace_id=principal.workspace_id,
                not_found_detail="tenant not found",
            )
            return await _erase_tenant(bootstrap, body.tenant_id)

        return await _erase_single_run(request, bootstrap, principal, body.run_id)

    async def _erase_single_run(
        request: Request,
        bootstrap: RetentionBootstrapLike,
        principal: AuthenticatedPrincipal,
        run_id: str,
    ) -> ErasureResponse:
        """Erase a single run after verifying tenant ownership (409 if held)."""
        tenant_id = principal.tenant_id
        # Ownership: the run (or its audits) must belong to the caller's tenant.
        await _require_run_tenant(request, bootstrap, run_id)
        try:
            result = await bootstrap.retention_erasure_service.erase_run(
                run_id, "rte", tenant_id=tenant_id
            )
        except LegalHoldError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return ErasureResponse(reason="rte", runs=[_run_result(result)])

    async def _erase_tenant(
        bootstrap: RetentionBootstrapLike,
        tenant_id: str,
    ) -> ErasureResponse:
        """Erase every erasable run for a tenant, skipping runs under legal hold."""
        from datetime import UTC, datetime

        holds = await bootstrap.legal_hold_repository.active_holds_for_tenant(tenant_id)
        if holds.tenant_wide:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="tenant is under an active tenant-wide legal hold",
            )
        # Everything created before "now" == every run for the tenant.
        records = await bootstrap.audit_repository.list_erasable(
            tenant_id, datetime.now(UTC), exclude_run_ids=holds.run_ids
        )
        run_ids = list(dict.fromkeys(record.run_id for record in records))
        results: list[ErasureRunResult] = []
        for run_id in run_ids:
            try:
                result = await bootstrap.retention_erasure_service.erase_run(
                    run_id, "rte", tenant_id=tenant_id
                )
            except LegalHoldError:
                continue
            results.append(_run_result(result))
        return ErasureResponse(reason="rte", runs=results)

    async def _require_run_tenant(
        request: Request,
        bootstrap: RetentionBootstrapLike,
        run_id: str,
    ) -> None:
        """404 (via resource-scope denial) if the run is not the caller's tenant."""
        run = await bootstrap.run_repository.get(run_id)
        if run is not None:
            await require_resource_scope(
                request,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                not_found_detail="run not found",
            )
            return
        records = await bootstrap.audit_repository.list(AuditQuery(run_id=run_id))
        if not records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        await require_resource_scope(
            request,
            tenant_id=records[0].tenant_id,
            workspace_id=records[0].workspace_id,
            not_found_detail="run not found",
        )


def _run_result(result: object) -> ErasureRunResult:
    """Map an erasure-service result onto the public per-run counts."""
    return ErasureRunResult(
        run_id=result.run_id,
        audits_erased=result.audits_erased,
        checkpoints_deleted=result.checkpoints_deleted,
        run_redacted=result.run_redacted,
        artifacts_deleted=result.artifacts_deleted,
        econ_events_deleted=result.econ_events_deleted,
    )

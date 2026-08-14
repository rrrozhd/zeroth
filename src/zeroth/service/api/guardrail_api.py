"""Tenant-safe operator API for durable ingress guardrail policy."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.governance.guardrails.policy import (
    EffectiveGuardrailSettings,
    GuardrailPolicyPatch,
    GuardrailPolicyRepository,
    GuardrailPolicyRevision,
    PolicyScope,
)
from zeroth.service.api.authorization import Permission, require_permission


class GuardrailPolicyResponse(BaseModel):
    """Latest explicit revisions and their composed effective settings."""

    model_config = ConfigDict(extra="forbid")

    tenant_revision: GuardrailPolicyRevision | None
    deployment_revision: GuardrailPolicyRevision | None
    effective: EffectiveGuardrailSettings


def register_guardrail_routes(app: FastAPI | APIRouter) -> None:
    """Register tenant and deployment guardrail management routes."""

    @app.get("/guardrails", response_model=GuardrailPolicyResponse)
    async def get_tenant_guardrails(request: Request) -> GuardrailPolicyResponse:
        await require_permission(request, Permission.DEPLOYMENT_READ)
        bootstrap = request.app.state.bootstrap
        return await _response(bootstrap, bootstrap.deployment.deployment_ref)

    @app.put("/guardrails", response_model=GuardrailPolicyResponse)
    async def put_tenant_guardrails(
        request: Request,
        body: GuardrailPolicyPatch,
    ) -> GuardrailPolicyResponse:
        principal = await require_permission(request, Permission.DEPLOYMENT_ADMIN)
        bootstrap = request.app.state.bootstrap
        await _append(bootstrap, "tenant", body, principal.subject)
        return await _response(bootstrap, bootstrap.deployment.deployment_ref)

    @app.get("/guardrails/history", response_model=list[GuardrailPolicyRevision])
    async def get_guardrail_history(request: Request) -> list[GuardrailPolicyRevision]:
        await require_permission(request, Permission.DEPLOYMENT_READ)
        return await _repository(request).history()

    @app.get(
        "/deployments/{deployment_ref}/guardrails",
        response_model=GuardrailPolicyResponse,
    )
    async def get_deployment_guardrails(
        request: Request,
        deployment_ref: str,
    ) -> GuardrailPolicyResponse:
        principal = await require_permission(
            request,
            Permission.DEPLOYMENT_READ,
            enforce_deployment_scope=False,
        )
        bootstrap = request.app.state.bootstrap
        await _require_deployment(bootstrap, deployment_ref, principal)
        return await _response(bootstrap, deployment_ref)

    @app.put(
        "/deployments/{deployment_ref}/guardrails",
        response_model=GuardrailPolicyResponse,
    )
    async def put_deployment_guardrails(
        request: Request,
        deployment_ref: str,
        body: GuardrailPolicyPatch,
    ) -> GuardrailPolicyResponse:
        principal = await require_permission(
            request,
            Permission.DEPLOYMENT_ADMIN,
            enforce_deployment_scope=False,
        )
        bootstrap = request.app.state.bootstrap
        await _require_deployment(bootstrap, deployment_ref, principal)
        await _append(bootstrap, "deployment", body, principal.subject, deployment_ref)
        return await _response(bootstrap, deployment_ref)


def _repository(request: Request) -> GuardrailPolicyRepository:
    repository = getattr(request.app.state.bootstrap, "guardrail_policy_repository", None)
    if not isinstance(repository, GuardrailPolicyRepository):
        raise HTTPException(status_code=503, detail="guardrail policy storage unavailable")
    return repository


async def _require_deployment(bootstrap, deployment_ref: str, principal) -> None:
    deployment = await bootstrap.deployment_service.get(
        deployment_ref,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")


async def _append(
    bootstrap,
    scope: PolicyScope,
    policy: GuardrailPolicyPatch,
    changed_by: str,
    deployment_ref: str | None = None,
) -> None:
    if not policy.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="policy must change at least one field",
        )
    repository = bootstrap.guardrail_policy_repository
    await repository.append(
        scope=scope,
        deployment_ref=deployment_ref,
        policy=policy,
        changed_by=changed_by,
    )
    metrics = getattr(bootstrap, "metrics_collector", None)
    if metrics is not None:
        metrics.increment("zeroth_guardrail_policy_changes_total", labels={"scope": scope})


async def _response(bootstrap, deployment_ref: str) -> GuardrailPolicyResponse:
    repository = bootstrap.guardrail_policy_repository
    return GuardrailPolicyResponse(
        tenant_revision=await repository.current("tenant"),
        deployment_revision=await repository.current("deployment", deployment_ref=deployment_ref),
        effective=await repository.effective(deployment_ref),
    )

"""Tenant-safe operator API for durable ingress guardrail policy."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, PrivateAttr, computed_field

from zeroth.governance.guardrails.policy import (
    EffectiveGuardrailSettings,
    GuardrailPolicyPatch,
    GuardrailPolicyRepository,
    GuardrailPolicyRevision,
    PolicyScope,
)
from zeroth.governance.identity import AuthenticatedPrincipal
from zeroth.service.api.authentication import record_service_denial
from zeroth.service.api.authorization import Permission, require_permission


class GuardrailPolicyResponse(BaseModel):
    """Latest explicit revisions and their composed effective settings."""

    model_config = ConfigDict(extra="forbid")

    tenant_revision: GuardrailPolicyRevision | None
    deployment_revision: GuardrailPolicyRevision | None
    effective: EffectiveGuardrailSettings
    _tenant_overrides: GuardrailPolicyPatch | None = PrivateAttr(default=None)
    _deployment_overrides: GuardrailPolicyPatch | None = PrivateAttr(default=None)

    @computed_field
    @property
    def tenant_overrides(self) -> GuardrailPolicyPatch | None:
        """Return the tenant scope's composed active overrides."""
        return self._tenant_overrides

    @computed_field
    @property
    def deployment_overrides(self) -> GuardrailPolicyPatch | None:
        """Return the deployment scope's composed active overrides."""
        return self._deployment_overrides


def register_guardrail_routes(app: FastAPI | APIRouter) -> None:
    """Register tenant and deployment guardrail management routes."""

    @app.get(
        "/guardrails",
        response_model=GuardrailPolicyResponse,
        response_model_exclude_unset=True,
    )
    async def get_tenant_guardrails(request: Request) -> GuardrailPolicyResponse:
        await _require_tenant_authority(request)
        bootstrap = request.app.state.bootstrap
        return await _response(bootstrap, bootstrap.deployment.deployment_ref)

    @app.put(
        "/guardrails",
        response_model=GuardrailPolicyResponse,
        response_model_exclude_unset=True,
    )
    async def put_tenant_guardrails(
        request: Request,
        body: GuardrailPolicyPatch,
    ) -> GuardrailPolicyResponse:
        principal = await _require_tenant_authority(request)
        bootstrap = request.app.state.bootstrap
        await _append(bootstrap, "tenant", body, principal.subject)
        return await _response(bootstrap, bootstrap.deployment.deployment_ref)

    @app.get(
        "/guardrails/history",
        response_model=list[GuardrailPolicyRevision],
        response_model_exclude_unset=True,
    )
    async def get_guardrail_history(request: Request) -> list[GuardrailPolicyRevision]:
        await _require_tenant_authority(request)
        return await _repository(request).history()

    @app.get(
        "/deployments/{deployment_ref}/guardrails",
        response_model=GuardrailPolicyResponse,
        response_model_exclude_unset=True,
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
        response_model_exclude_unset=True,
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


async def _require_tenant_authority(request: Request) -> AuthenticatedPrincipal:
    """Require unscoped authority for the deployment's whole tenant."""
    principal = await require_permission(
        request,
        Permission.GUARDRAIL_TENANT_ADMIN,
        enforce_deployment_scope=False,
    )
    bootstrap = request.app.state.bootstrap
    deployment = bootstrap.deployment
    if principal.tenant_id == deployment.tenant_id and principal.workspace_id is None:
        return principal
    await record_service_denial(
        audit_repository=getattr(bootstrap, "audit_repository", None),
        deployment=deployment,
        request=request,
        node_id="service.authorization",
        status="forbidden",
        error="tenant-wide guardrail scope mismatch",
        actor=principal.to_actor(),
        metadata={"permission": Permission.GUARDRAIL_TENANT_ADMIN.value},
    )
    if principal.tenant_id != deployment.tenant_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    raise HTTPException(status_code=403, detail="forbidden")


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
    if not policy.has_changes():
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
    response = GuardrailPolicyResponse(
        tenant_revision=await repository.latest("tenant"),
        deployment_revision=await repository.latest("deployment", deployment_ref=deployment_ref),
        effective=await repository.effective(deployment_ref),
    )
    response._tenant_overrides = await repository.current("tenant")
    response._deployment_overrides = await repository.current(
        "deployment", deployment_ref=deployment_ref
    )
    return response

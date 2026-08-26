"""Certification registration, readiness, promotion, revocation, and overrides."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from zeroth.platform.primitives import utc_now
from zeroth.service.api.authorization import Permission, require_permission
from zeroth.service.certifications.models import (
    AppCertification,
    CertificationEvaluation,
    CertificationEvent,
    CertificationOverride,
    OverrideScope,
    PromotionConflictError,
    PromotionRejectedError,
    ServingArtifactIdentity,
)
from zeroth.service.certifications.receipt import SignedPromotionReceipt


class RegisterCertificationRequest(BaseModel):
    """A portable certification receipt to retain and evaluate."""

    model_config = ConfigDict(extra="forbid")
    receipt: SignedPromotionReceipt


class PromoteCertificationRequest(BaseModel):
    """Promotion intent; the target and artifact identity are server-owned."""

    model_config = ConfigDict(extra="forbid")


class RevokeCertificationRequest(BaseModel):
    """Operator reason retained with an explicit revocation."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class CertificationOverrideRequest(BaseModel):
    """Authorized, scoped, expiring exception request."""

    model_config = ConfigDict(extra="forbid")
    scopes: tuple[OverrideScope, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    expires_at: AwareDatetime

    @field_validator("reason")
    @classmethod
    def _reason_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class CertificationResponse(BaseModel):
    """One certification with the central decision and audit evidence."""

    model_config = ConfigDict(extra="forbid")
    certification_id: str
    tenant_id: str
    workspace_id: str | None
    app_name: str
    app_commit: str
    image_digest: str
    state: str
    promotion_target_key: str | None
    override: CertificationOverride | None
    evaluation: CertificationEvaluation
    events: tuple[CertificationEvent, ...]
    created_at: datetime
    updated_at: datetime


def _service(request: Request):
    service = getattr(request.app.state.bootstrap, "certification_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="certification service unavailable")
    return service


def _artifact_identity(request: Request) -> ServingArtifactIdentity | None:
    """Return only the typed server-owned identity attached during bootstrap."""
    identity = getattr(request.app.state.bootstrap, "serving_artifact_identity", None)
    return identity if isinstance(identity, ServingArtifactIdentity) else None


async def _response(request: Request, service, record: AppCertification) -> CertificationResponse:
    payload = record.receipt.payload
    events = await service.events(record.certification_id, record.tenant_id, record.workspace_id)
    return CertificationResponse(
        certification_id=record.certification_id,
        tenant_id=record.tenant_id,
        workspace_id=record.workspace_id,
        app_name=payload.app_name,
        app_commit=payload.app_commit,
        image_digest=payload.image_digest,
        state=record.state.value,
        promotion_target_key=record.promotion_target_key,
        override=record.override,
        evaluation=service.evaluate(
            record,
            environment="production",
            now=utc_now(),
            artifact_identity=_artifact_identity(request),
        ),
        events=tuple(events),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def register_certification_routes(app: FastAPI | APIRouter) -> None:
    """Register the certification control plane."""

    @app.post("/certifications", response_model=CertificationResponse, status_code=201)
    async def create_certification(
        request: Request, body: RegisterCertificationRequest
    ) -> CertificationResponse:
        principal = await require_permission(
            request, Permission.DEPLOYMENT_ADMIN, enforce_deployment_scope=False
        )
        payload = body.receipt.payload
        if (payload.tenant_id, payload.workspace_id) != (
            principal.tenant_id,
            principal.workspace_id,
        ):
            raise HTTPException(status_code=403, detail="receipt scope does not match principal")
        try:
            record = await _service(request).register(
                body.receipt, actor_id=principal.subject, now=utc_now()
            )
        except PromotionRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="certification already exists") from exc
            raise
        return await _response(request, _service(request), record)

    @app.get("/certifications", response_model=list[CertificationResponse])
    async def list_certifications(request: Request) -> list[CertificationResponse]:
        principal = await require_permission(
            request, Permission.DEPLOYMENT_READ, enforce_deployment_scope=False
        )
        service = _service(request)
        records = await service.list(principal.tenant_id, principal.workspace_id)
        return [await _response(request, service, record) for record in records]

    @app.get("/certifications/{certification_id}", response_model=CertificationResponse)
    async def get_certification(
        request: Request, certification_id: str
    ) -> CertificationResponse:
        principal = await require_permission(
            request, Permission.DEPLOYMENT_READ, enforce_deployment_scope=False
        )
        service = _service(request)
        record = await service.get(
            certification_id, principal.tenant_id, principal.workspace_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="certification not found")
        return await _response(request, service, record)

    @app.post(
        "/certifications/{certification_id}/promote",
        response_model=CertificationResponse,
    )
    async def promote_certification(
        request: Request,
        certification_id: str,
        body: PromoteCertificationRequest,
    ) -> CertificationResponse:
        principal = await require_permission(
            request, Permission.DEPLOYMENT_ADMIN, enforce_deployment_scope=False
        )
        service = _service(request)
        try:
            record = await service.promote(
                certification_id,
                principal.tenant_id,
                principal.workspace_id,
                artifact_identity=_artifact_identity(request),
                actor_id=principal.subject,
                now=utc_now(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="certification not found") from exc
        except (PromotionRejectedError, PromotionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _response(request, service, record)

    @app.post(
        "/certifications/{certification_id}/revoke",
        response_model=CertificationResponse,
    )
    async def revoke_certification(
        request: Request,
        certification_id: str,
        body: RevokeCertificationRequest,
    ) -> CertificationResponse:
        principal = await require_permission(
            request, Permission.DEPLOYMENT_ADMIN, enforce_deployment_scope=False
        )
        service = _service(request)
        try:
            record = await service.revoke(
                certification_id,
                principal.tenant_id,
                principal.workspace_id,
                reason=body.reason,
                actor_id=principal.subject,
                now=utc_now(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="certification not found") from exc
        return await _response(request, service, record)

    @app.post(
        "/certifications/{certification_id}/override",
        response_model=CertificationResponse,
    )
    async def override_certification(
        request: Request,
        certification_id: str,
        body: CertificationOverrideRequest,
    ) -> CertificationResponse:
        principal = await require_permission(
            request, Permission.CERTIFICATION_OVERRIDE, enforce_deployment_scope=False
        )
        service = _service(request)
        try:
            record = await service.grant_override(
                certification_id,
                principal.tenant_id,
                principal.workspace_id,
                scopes=body.scopes,
                reason=body.reason,
                expires_at=body.expires_at,
                actor_id=principal.subject,
                now=utc_now(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="certification not found") from exc
        except PromotionRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _response(request, service, record)


__all__ = ["CertificationResponse", "register_certification_routes"]

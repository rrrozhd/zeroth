"""Durable certification, promotion, and operator-facing blocker models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from zeroth.service.certifications.receipt import SignedPromotionReceipt


class CertificationState(StrEnum):
    """Lifecycle state for one immutable candidate identity."""

    BUILDABLE = "buildable"
    TEST_DEPLOYABLE = "test_deployable"
    CERTIFIED = "certified"
    PROMOTED = "promoted"
    REVOKED = "revoked"


class OverrideScope(StrEnum):
    """Explicitly overridable production-readiness blockers."""

    RECEIPT_EXPIRED = "receipt_expired"
    ENVIRONMENT_POLICY = "environment_policy"


class ServingArtifactIdentity(BaseModel):
    """Server-owned identity of the deployment artifact currently being served."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_key: str = Field(min_length=1, max_length=255)
    app_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("target_key")
    @classmethod
    def _target_nonblank(cls, value: str) -> str:
        """Reject whitespace-only deployment target keys."""
        if not value.strip():
            raise ValueError("target_key must not be blank")
        return value


class CertificationOverride(BaseModel):
    """Time-bound administrative exception retained with the certification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scopes: tuple[OverrideScope, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    actor_id: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("reason", "actor_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class AppCertification(BaseModel):
    """Persisted runtime certification record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certification_id: str
    tenant_id: str
    workspace_id: str | None = None
    receipt: SignedPromotionReceipt
    state: CertificationState
    promotion_target_key: str | None = None
    promoted_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    override: CertificationOverride | None = None
    created_at: datetime
    updated_at: datetime


class CertificationEvent(BaseModel):
    """One append-only certification audit event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    certification_id: str
    tenant_id: str
    workspace_id: str | None = None
    event_type: str
    state: CertificationState
    actor_id: str
    reason: str | None = None
    scopes: tuple[OverrideScope, ...] = ()
    created_at: datetime


class CertificationBlocker(BaseModel):
    """Machine-readable blocker paired with a console remediation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    remediation: str
    overridable: bool = False


class CertificationEvaluation(BaseModel):
    """Central readiness decision reused by API, probes, metrics, and console."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certification_id: str | None = None
    state: CertificationState | None = None
    test_deployable: bool
    production_ready: bool
    override_active: bool = False
    blockers: tuple[CertificationBlocker, ...] = ()


class CertificationError(RuntimeError):
    """Base class for certification boundary failures."""


class PromotionRejectedError(CertificationError):
    """The candidate did not satisfy a promotion prerequisite."""


class PromotionConflictError(CertificationError):
    """Another certification already owns the production target."""


def state_for_environments(environments: tuple[str, ...]) -> CertificationState:
    """Return the strongest pre-promotion state justified by the receipt."""
    if "production" in environments:
        return CertificationState.CERTIFIED
    if "test" in environments:
        return CertificationState.TEST_DEPLOYABLE
    return CertificationState.BUILDABLE


__all__ = [
    "AppCertification",
    "CertificationBlocker",
    "CertificationError",
    "CertificationEvaluation",
    "CertificationEvent",
    "CertificationOverride",
    "CertificationState",
    "OverrideScope",
    "PromotionConflictError",
    "PromotionRejectedError",
    "ServingArtifactIdentity",
    "state_for_environments",
]

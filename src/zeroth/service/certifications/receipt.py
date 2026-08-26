"""Portable signed receipt for one app-certification candidate."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from zeroth.platform.signing import SigningKeyProvider, sign_digest, verify_digest
from zeroth.platform.storage.json import to_json_value

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"
_VERSION = r"^[0-9]+(?:\.[0-9]+){1,4}$"
_IMAGE_REFERENCE = r"^[A-Za-z0-9][A-Za-z0-9./:_-]*$"


class PromotionReceiptPayload(BaseModel):
    """Immutable certification claims accepted by the runtime promotion gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    certification_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tenant_id: str = Field(min_length=1, max_length=255)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=255)
    app_name: str = Field(min_length=1, max_length=120)
    app_commit: str = Field(pattern=_COMMIT)
    zeroth_version: str = Field(pattern=_VERSION)
    image_reference: str = Field(pattern=_IMAGE_REFERENCE, max_length=255)
    image_digest: str = Field(pattern=_DIGEST)
    source_digest: str = Field(pattern=_DIGEST)
    evidence_digest: str = Field(pattern=_DIGEST)
    report_digest: str = Field(pattern=_DIGEST)
    environments: tuple[Literal["test", "production"], ...] = Field(max_length=2)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("tenant_id", "workspace_id", "app_name")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("environments")
    @classmethod
    def _unique_environments(
        cls, value: tuple[Literal["test", "production"], ...]
    ) -> tuple[Literal["test", "production"], ...]:
        if len(value) != len(set(value)):
            raise ValueError("environments must be unique")
        return value

    @model_validator(mode="after")
    def _valid_window(self) -> PromotionReceiptPayload:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


class SignedPromotionReceipt(BaseModel):
    """Certification payload plus a keyed signature over its canonical digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: PromotionReceiptPayload
    digest: str = Field(pattern=_DIGEST)
    signature: str | None = None
    signing_key_id: str | None = None
    signing_algorithm: str | None = None


def receipt_digest(payload: PromotionReceiptPayload) -> str:
    """Return the canonical SHA-256 identity of a receipt payload."""
    return "sha256:" + hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()


def sign_promotion_receipt(
    payload: PromotionReceiptPayload,
    signer: SigningKeyProvider | None,
) -> SignedPromotionReceipt:
    """Sign one exact promotion receipt payload."""
    digest = receipt_digest(payload)
    signature, key_id, algorithm = sign_digest(digest, signer)
    return SignedPromotionReceipt(
        payload=payload,
        digest=digest,
        signature=signature,
        signing_key_id=key_id,
        signing_algorithm=algorithm,
    )


def verify_promotion_receipt(
    receipt: SignedPromotionReceipt,
    verifier: SigningKeyProvider | None,
) -> bool:
    """Fail closed unless every claim and the keyed signature remain exact."""
    return promotion_receipt_verification(receipt, verifier) == "valid"


def promotion_receipt_verification(
    receipt: SignedPromotionReceipt,
    verifier: SigningKeyProvider | None,
) -> Literal["valid", "invalid", "unavailable"]:
    """Distinguish invalid evidence from an unavailable verifier."""
    try:
        digest = receipt_digest(receipt.payload)
        if (
            digest != receipt.digest
            or not receipt.signature
            or not receipt.signing_key_id
            or not receipt.signing_algorithm
        ):
            return "invalid"
        if verifier is None:
            return "unavailable"
        return (
            "valid"
            if verify_digest(
                digest,
                receipt.signature,
                receipt.signing_key_id,
                receipt.signing_algorithm,
                verifier,
            )
            else "invalid"
        )
    except Exception:  # noqa: BLE001 - untrusted receipt boundary
        return "unavailable"

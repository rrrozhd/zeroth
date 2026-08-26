"""Issue a runtime-portable receipt from retained certification evidence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from zeroth.platform.signing import SigningKeyProvider
from zeroth.service.certifications.receipt import (
    PromotionReceiptPayload,
    SignedPromotionReceipt,
    sign_promotion_receipt,
)

from .evidence import verify_finalized_attestation
from .models import evidence_binding_digest, file_digest


def issue_promotion_receipt(
    report_path: Path,
    *,
    root: Path,
    signer: SigningKeyProvider,
    tenant_id: str,
    workspace_id: str | None = None,
    environments: tuple[Literal["test", "production"], ...],
    issued_at: datetime,
    expires_at: datetime,
    certification_id: str | None = None,
    repository: str | None = None,
    signer_repo: str | None = None,
    signer_workflow: str | None = None,
    signer_digest: str | None = None,
) -> SignedPromotionReceipt:
    """Validate retained evidence and sign its exact candidate identity."""
    report = verify_finalized_attestation(
        report_path,
        root,
        repository=repository,
        signer_repo=signer_repo,
        signer_workflow=signer_workflow,
        signer_digest=signer_digest,
    )
    if report.status != "passed" or report.candidate is None or report.evidence is None:
        raise ValueError("promotion receipt requires a passing candidate-bound report")
    candidate = report.candidate
    receipt = sign_promotion_receipt(
        PromotionReceiptPayload(
            certification_id=certification_id or uuid4().hex,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            app_name=candidate.app_name,
            app_commit=candidate.app_commit,
            zeroth_version=candidate.zeroth_version,
            image_reference=candidate.image_reference,
            image_digest=candidate.image_digest,
            source_digest=candidate.source_digest,
            evidence_digest=evidence_binding_digest(report.evidence),
            report_digest=file_digest(report_path),
            environments=environments,
            issued_at=issued_at,
            expires_at=expires_at,
        ),
        signer,
    )
    if receipt.signature is None:
        raise ValueError("promotion receipt signer did not produce a signature")
    return receipt

"""Central certification evaluator and production-promotion boundary."""

from __future__ import annotations

from datetime import datetime

from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.primitives import utc_now
from zeroth.platform.signing import SigningKeyProvider
from zeroth.service.certifications.models import (
    AppCertification,
    CertificationBlocker,
    CertificationEvaluation,
    CertificationOverride,
    CertificationState,
    OverrideScope,
    PromotionRejectedError,
    ServingArtifactIdentity,
    state_for_environments,
)
from zeroth.service.certifications.receipt import (
    SignedPromotionReceipt,
    promotion_receipt_verification,
    verify_promotion_receipt,
)
from zeroth.service.certifications.repository import CertificationRepository

_OVERRIDE_CODES = {
    "receipt_expired": OverrideScope.RECEIPT_EXPIRED,
    "environment_not_certified": OverrideScope.ENVIRONMENT_POLICY,
}


class CertificationService:
    """Verify receipts, evaluate readiness, and coordinate durable transitions."""

    def __init__(
        self,
        repository: CertificationRepository,
        *,
        verifier: SigningKeyProvider | None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.repository = repository
        self._verifier = verifier
        self._metrics = metrics

    async def register(
        self, receipt: SignedPromotionReceipt, *, actor_id: str, now: datetime
    ) -> AppCertification:
        """Persist a trusted signed receipt in its justified initial state."""
        if not verify_promotion_receipt(receipt, self._verifier):
            self._metric("register", "rejected")
            raise PromotionRejectedError("receipt signature is invalid or untrusted")
        payload = receipt.payload
        record = AppCertification(
            certification_id=payload.certification_id,
            tenant_id=payload.tenant_id,
            workspace_id=payload.workspace_id,
            receipt=receipt,
            state=state_for_environments(tuple(payload.environments)),
            created_at=now,
            updated_at=now,
        )
        created = await self.repository.create(record, actor_id=actor_id)
        self._metric("register", "accepted")
        return created

    async def get(
        self, certification_id: str, tenant_id: str, workspace_id: str | None
    ) -> AppCertification | None:
        """Load a scoped certification."""
        record = await self.repository.get(certification_id, tenant_id, workspace_id)
        return None if record is None else await self._revoke_invalid(record)

    async def list(
        self, tenant_id: str, workspace_id: str | None
    ) -> list[AppCertification]:
        """List scoped certifications."""
        records = await self.repository.list(tenant_id, workspace_id)
        return [await self._revoke_invalid(record) for record in records]

    async def events(
        self, certification_id: str, tenant_id: str, workspace_id: str | None
    ):
        """Return the append-only audit timeline."""
        return await self.repository.events(certification_id, tenant_id, workspace_id)

    def evaluate(
        self,
        record: AppCertification,
        *,
        environment: str,
        now: datetime,
        artifact_identity: ServingArtifactIdentity | None = None,
    ) -> CertificationEvaluation:
        """Return the decision consumed by API, probes, metrics, and console."""
        payload = record.receipt.payload
        blockers: list[CertificationBlocker] = []
        if record.state is CertificationState.REVOKED:
            blockers.append(
                CertificationBlocker(
                    code="certification_revoked",
                    message=record.revocation_reason or "Certification has been revoked.",
                    remediation="Create and sign a new certification receipt for the artifact.",
                )
            )
        else:
            verification = promotion_receipt_verification(record.receipt, self._verifier)
        if record.state is not CertificationState.REVOKED and verification == "invalid":
            blockers.append(
                CertificationBlocker(
                    code="receipt_signature_invalid",
                    message="The certification receipt signature is invalid or untrusted.",
                    remediation="Reissue the receipt with a currently trusted certification key.",
                )
            )
        elif record.state is not CertificationState.REVOKED and verification == "unavailable":
            blockers.append(
                CertificationBlocker(
                    code="receipt_verifier_unavailable",
                    message="The certification receipt verifier is unavailable.",
                    remediation="Restore the configured certification trust provider and retry.",
                )
            )
        if environment == "production":
            if record.state is not CertificationState.PROMOTED or not record.promotion_target_key:
                blockers.append(
                    CertificationBlocker(
                        code="production_not_promoted",
                        message="The certification has not been promoted to a production target.",
                        remediation=(
                            "Promote the certification to the exact serving deployment target."
                        ),
                    )
                )
            elif artifact_identity is None:
                blockers.append(
                    CertificationBlocker(
                        code="serving_artifact_identity_unavailable",
                        message="The serving artifact identity could not be verified.",
                        remediation=("Configure the server-owned serving commit and image digest."),
                    )
                )
            elif artifact_identity.target_key != record.promotion_target_key:
                blockers.append(
                    CertificationBlocker(
                        code="promotion_target_mismatch",
                        message="The serving target does not match the promoted target.",
                        remediation=(
                            "Promote a valid receipt to the exact serving deployment target."
                        ),
                    )
                )
        if artifact_identity is not None and artifact_identity.app_commit != payload.app_commit:
            blockers.append(
                CertificationBlocker(
                    code="commit_mismatch",
                    message="The promotion commit does not match the certified commit.",
                    remediation="Certify the exact commit being promoted.",
                )
            )
        if artifact_identity is not None and artifact_identity.image_digest != payload.image_digest:
            blockers.append(
                CertificationBlocker(
                    code="image_digest_mismatch",
                    message="The promotion image digest does not match the certified image.",
                    remediation="Certify the exact immutable image digest being promoted.",
                )
            )
        if now >= payload.expires_at:
            blockers.append(
                CertificationBlocker(
                    code="receipt_expired",
                    message="The certification receipt has expired.",
                    remediation="Re-run certification or request a time-bound expiry override.",
                    overridable=True,
                )
            )
        if environment not in payload.environments:
            blockers.append(
                CertificationBlocker(
                    code="environment_not_certified",
                    message=f"The receipt does not authorize the {environment} environment.",
                    remediation=(
                        f"Certify the artifact for {environment} or request a scoped policy "
                        "override."
                    ),
                    overridable=True,
                )
            )
        active_override = record.override is not None and now < record.override.expires_at
        if active_override:
            scopes = set(record.override.scopes)
            blockers = [
                blocker
                for blocker in blockers
                if _OVERRIDE_CODES.get(blocker.code) not in scopes
            ]
        return CertificationEvaluation(
            certification_id=record.certification_id,
            state=record.state,
            test_deployable=(
                "test" in payload.environments
                and record.state is not CertificationState.REVOKED
            ),
            production_ready=(
                environment == "production"
                and record.state is CertificationState.PROMOTED
                and artifact_identity is not None
                and record.promotion_target_key == artifact_identity.target_key
                and not blockers
            ),
            override_active=active_override,
            blockers=tuple(blockers),
        )

    async def promote(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        *,
        artifact_identity: ServingArtifactIdentity | None,
        actor_id: str,
        now: datetime,
    ) -> AppCertification:
        """Revoke identity drift, then atomically claim a production target."""
        record = await self.get(certification_id, tenant_id, workspace_id)
        if record is None:
            raise KeyError(certification_id)
        if artifact_identity is None:
            self._metric("promote", "blocked")
            raise PromotionRejectedError("serving artifact identity is unavailable")
        payload = record.receipt.payload
        mismatch: tuple[str, str] | None = None
        if artifact_identity.app_commit != payload.app_commit:
            mismatch = ("commit", "promotion commit does not match certified commit")
        elif artifact_identity.image_digest != payload.image_digest:
            mismatch = (
                "image digest",
                "promotion image digest does not match certified image digest",
            )
        if mismatch is not None:
            await self.repository.revoke(
                certification_id,
                tenant_id,
                workspace_id,
                reason=mismatch[1],
                actor_id=actor_id,
                at=now,
            )
            self._metric("promote", f"{mismatch[0].replace(' ', '_')}_mismatch")
            raise PromotionRejectedError(
                f"promotion {mismatch[0]} mismatch revoked certification"
            )
        evaluation = self.evaluate(
            record,
            environment="production",
            now=now,
            artifact_identity=artifact_identity,
        )
        admission_blockers = tuple(
            blocker for blocker in evaluation.blockers if blocker.code != "production_not_promoted"
        )
        if admission_blockers:
            self._metric("promote", "blocked")
            blocker = admission_blockers[0]
            raise PromotionRejectedError(blocker.message)
        promoted = await self.repository.promote(
            certification_id,
            tenant_id,
            workspace_id,
            target_key=artifact_identity.target_key,
            actor_id=actor_id,
            at=now,
        )
        self._metric("promote", "promoted")
        return promoted

    async def revoke(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        *,
        reason: str,
        actor_id: str,
        now: datetime,
    ) -> AppCertification:
        """Explicitly revoke a certification."""
        record = await self.repository.revoke(
            certification_id,
            tenant_id,
            workspace_id,
            reason=reason,
            actor_id=actor_id,
            at=now,
        )
        self._metric("revoke", "revoked")
        return record

    async def grant_override(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        *,
        scopes: tuple[OverrideScope, ...],
        reason: str,
        expires_at: datetime,
        actor_id: str,
        now: datetime,
    ) -> AppCertification:
        """Grant a visible, scoped, time-bound exception."""
        if expires_at <= now:
            raise PromotionRejectedError("override expiry must be in the future")
        record = await self.get(certification_id, tenant_id, workspace_id)
        if record is None:
            raise KeyError(certification_id)
        if record.state is CertificationState.REVOKED:
            raise PromotionRejectedError("revoked certification cannot be overridden")
        if not verify_promotion_receipt(record.receipt, self._verifier):
            raise PromotionRejectedError("invalid receipt signature cannot be overridden")
        override = CertificationOverride(
            scopes=scopes,
            reason=reason,
            actor_id=actor_id,
            created_at=now,
            expires_at=expires_at,
        )
        record = await self.repository.grant_override(
            certification_id,
            tenant_id,
            workspace_id,
            override,
            actor_id=actor_id,
            at=now,
        )
        self._metric("override", "granted")
        return record

    async def production_readiness(
        self,
        artifact_identity: ServingArtifactIdentity | None,
        tenant_id: str,
        workspace_id: str | None,
        *,
        now: datetime,
    ) -> CertificationEvaluation:
        """Fail closed for missing state or a storage verification fault."""
        if artifact_identity is None:
            evaluation = _missing_readiness(
                "serving_artifact_identity_unavailable",
                "The serving artifact identity could not be verified.",
                "Configure the server-owned serving commit and image digest.",
            )
            self._readiness_metric(evaluation)
            return evaluation
        try:
            record = await self.repository.get_by_target(
                artifact_identity.target_key, tenant_id, workspace_id
            )
            if record is not None:
                record = await self._revoke_invalid(record)
            if record is not None and record.state is not CertificationState.REVOKED:
                mismatch = _artifact_mismatch(record, artifact_identity)
                if mismatch is not None:
                    record = await self.repository.revoke(
                        record.certification_id,
                        tenant_id,
                        workspace_id,
                        reason=mismatch,
                        actor_id="system:serving-artifact-verifier",
                        at=now,
                    )
        except Exception:  # noqa: BLE001 - health must fail closed
            evaluation = _missing_readiness(
                "certification_storage_unavailable",
                "Certification state could not be verified.",
                "Restore certification storage and retry readiness.",
            )
        else:
            if record is None:
                evaluation = _missing_readiness(
                    "production_not_promoted",
                    "No promoted certification owns this production target.",
                    "Register a trusted production receipt and promote it to this target.",
                )
            else:
                evaluation = self.evaluate(
                    record,
                    environment="production",
                    now=now,
                    artifact_identity=artifact_identity,
                )
        self._readiness_metric(evaluation)
        return evaluation

    def _metric(self, operation: str, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(
                "zeroth_certification_operations_total",
                labels={"operation": operation, "outcome": outcome},
            )

    def _readiness_metric(self, evaluation: CertificationEvaluation) -> None:
        if self._metrics is not None:
            self._metrics.gauge_set(
                "zeroth_production_ready", 1.0 if evaluation.production_ready else 0.0
            )

    async def _revoke_invalid(self, record: AppCertification) -> AppCertification:
        """Release a target when retained evidence is cryptographically invalid."""
        if (
            record.state is not CertificationState.REVOKED
            and promotion_receipt_verification(record.receipt, self._verifier) == "invalid"
        ):
            return await self.repository.revoke(
                record.certification_id,
                record.tenant_id,
                record.workspace_id,
                reason="certification receipt evidence is invalid",
                actor_id="system:certification-verifier",
                at=utc_now(),
            )
        return record


def _missing_readiness(code: str, message: str, remediation: str) -> CertificationEvaluation:
    return CertificationEvaluation(
        test_deployable=True,
        production_ready=False,
        blockers=(
            CertificationBlocker(
                code=code,
                message=message,
                remediation=remediation,
            ),
        ),
    )


def _artifact_mismatch(
    record: AppCertification, artifact_identity: ServingArtifactIdentity
) -> str | None:
    """Describe serving identity drift from the signed receipt, if any."""
    payload = record.receipt.payload
    if artifact_identity.app_commit != payload.app_commit:
        return "serving app commit no longer matches the certified commit"
    if artifact_identity.image_digest != payload.image_digest:
        return "serving image digest no longer matches the certified image digest"
    return None


__all__ = ["CertificationService"]

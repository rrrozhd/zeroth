"""Legacy per-step retention log entries emitted alongside the cleanup manifest.

These entries predate the manifest and are kept so operators' existing queries
over ``retention_audit_log`` -- ``crypto_erase_audits``, ``artifact_cleanup``,
``erase_run_complete`` and friends -- keep returning rows.

Every write here is best-effort and failures are logged rather than raised. By
the time these run the data is already destroyed, so there is nothing a caller
could do with the exception except turn a successful erasure into a reported
failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
    from zeroth.core.retention.cleanup_manifest import CleanupManifest
    from zeroth.core.retention.models import ErasureResult

logger = logging.getLogger(__name__)


def result_detail(result: ErasureResult) -> dict[str, Any]:
    """Serialize an :class:`ErasureResult` into the ``erase_run_complete`` detail payload."""
    return {
        "audits_erased": result.audits_erased,
        "checkpoints_deleted": result.checkpoints_deleted,
        "run_redacted": result.run_redacted,
        "artifacts_deleted": result.artifacts_deleted,
        "econ_events_deleted": result.econ_events_deleted,
        "external_cleanup_status": result.external_cleanup_status,
        "authorization_log_id": result.authorization_log_id,
        "retry_log_id": result.retry_log_id,
    }


@dataclass(frozen=True, slots=True)
class CompatibilityLog:
    """Writes the pre-manifest retention log entries for one erasure."""

    log: RetentionAuditLogRepository

    async def record_database_steps(self, result: ErasureResult) -> None:
        """Emit legacy per-step database log entries (best-effort; failures only logged)."""
        for action, detail in (
            ("crypto_erase_audits", {"count": result.audits_erased}),
            ("erase_checkpoints", {"count": result.checkpoints_deleted}),
            ("redact_run", {"redacted": result.run_redacted}),
        ):
            await self.record(result, action, detail)

    async def record_external_steps(
        self,
        result: ErasureResult,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> None:
        """Emit legacy artifact/econ/completion log entries mirroring pre-manifest logging."""
        await self.record(result, "artifact_cleanup", {"count": result.artifacts_deleted})
        econ = next(
            (operation for operation in manifest.operations if operation.kind == "econ"),
            None,
        )
        if econ is None:
            if not failed:
                await self.record(result, "erase_run_complete", result_detail(result))
            return
        econ_action = "econ_erase_skipped"
        if econ.status == "completed":
            econ_action = "econ_erase"
        elif econ.status == "failed":
            econ_action = "econ_erase_failed"
        await self.record(
            result,
            econ_action,
            {
                "count": result.econ_events_deleted,
                "join_keys": econ.join_keys,
            },
        )
        if not failed:
            await self.record(result, "erase_run_complete", result_detail(result))

    async def record(
        self,
        result: ErasureResult,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        """Write one best-effort compatibility log entry; failures are logged, never raised."""
        try:
            await self.log.record(
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                action=action,
                reason=result.reason,
                detail=detail,
            )
        except Exception:
            logger.exception("best-effort compatibility log %s failed", action)

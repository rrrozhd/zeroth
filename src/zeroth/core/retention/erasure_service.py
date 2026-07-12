"""RetentionErasureService (WS-E): full-surface, legal-hold-aware erasure.

Right-to-erasure removes a run's PII across every surface WITHOUT breaking the
append-only audit hash-chain:

* node audits    -> crypto-erased (plaintext nulled, commitment digest kept, so
                    the chain still verifies)
* run checkpoints -> deleted (the richest plaintext snapshot; the missing cascade)
* runs row       -> redacted in place (output columns nulled, row kept)
* artifacts      -> cleaned up by run prefix + per-key delete of references found
                    in the (pre-erasure) output snapshots
* econ events    -> deleted via the optional econ hook (best-effort join keys)

Every step is idempotent and recorded in ``retention_audit_log``. A legal hold on
the run (or a tenant-wide hold) beats BOTH TTL purge and explicit erasure: the
service refuses and raises :class:`LegalHoldError`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zeroth.core.retention.models import ErasureResult

if TYPE_CHECKING:
    from zeroth.core.audit.repository import AuditRepository
    from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
    from zeroth.core.retention.econ_eraser import EconEventEraser
    from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository
    from zeroth.core.runs.repository import RunRepository

logger = logging.getLogger(__name__)


class LegalHoldError(RuntimeError):
    """Raised when erasure is refused because an active legal hold covers it."""


def _harvest_artifact_keys(payload: Any) -> set[str]:
    """Recursively collect ArtifactReference keys embedded in a snapshot.

    An artifact reference serializes to a dict carrying both ``store`` and
    ``key``; we pull the ``key`` so the erasure service can per-key delete the
    externalized blob even if run-prefix cleanup misses it.
    """
    found: set[str] = set()
    if isinstance(payload, dict):
        if isinstance(payload.get("store"), str) and isinstance(payload.get("key"), str):
            found.add(payload["key"])
        for value in payload.values():
            found |= _harvest_artifact_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= _harvest_artifact_keys(item)
    return found


class RetentionErasureService:
    """Coordinates legal-hold-aware, full-surface run erasure."""

    def __init__(
        self,
        *,
        audit_repository: AuditRepository,
        run_repository: RunRepository,
        policy_repository: RetentionPolicyRepository,
        legal_hold_repository: LegalHoldRepository,
        log_repository: RetentionAuditLogRepository,
        artifact_store: object | None = None,
        econ_eraser: EconEventEraser | None = None,
    ) -> None:
        self._audits = audit_repository
        self._runs = run_repository
        self._policies = policy_repository
        self._holds = legal_hold_repository
        self._log = log_repository
        self._artifact_store = artifact_store
        self._econ_eraser = econ_eraser

    async def erase_run(
        self,
        run_id: str,
        reason: str,
        *,
        tenant_id: str | None = None,
    ) -> ErasureResult:
        """Erase every PII surface for a run, unless an active legal hold blocks.

        ``reason`` is one of ``ttl`` | ``rte`` | ``manual``. Idempotent: re-running
        deletes nothing already gone and still returns a result. Raises
        :class:`LegalHoldError` when the run (or its tenant) is on hold.
        """
        records = await self._audits.list_by_run(run_id)
        resolved_tenant = await self._resolve_tenant(run_id, tenant_id, records)

        holds = await self._holds.active_holds_for_tenant(resolved_tenant)
        if holds.blocks(run_id):
            await self._log.record(
                tenant_id=resolved_tenant,
                run_id=run_id,
                action="erasure_refused_legal_hold",
                reason=reason,
            )
            raise LegalHoldError(
                f"run {run_id!r} is under an active legal hold and cannot be erased"
            )

        # Harvest artifact keys from the PII-intact output snapshots BEFORE
        # crypto-erase nulls them, or the keys are gone.
        artifact_keys: set[str] = set()
        join_keys: set[str] = {run_id}
        for record in records:
            artifact_keys |= _harvest_artifact_keys(record.output_snapshot)
            metadata_join = record.execution_metadata.get("join_key")
            if isinstance(metadata_join, str) and metadata_join:
                join_keys.add(metadata_join)

        result = ErasureResult(run_id=run_id, tenant_id=resolved_tenant, reason=reason)

        # 1. Crypto-erase every node audit (legacy v1 rows are un-erasable and
        #    skipped, not fatal — mixed chains still verify). ``audits_erased``
        #    counts only records NEWLY erased this call, so a re-run reports 0.
        for record in records:
            if record.erased:
                continue  # already a tombstone (idempotent re-run)
            try:
                erased = await self._audits.crypto_erase(record.audit_id, reason=reason)
            except ValueError:
                logger.info(
                    "skipping legacy (digest_version=1) audit %s during erasure of run %s",
                    record.audit_id,
                    run_id,
                )
                continue
            if erased is not None:
                result.audits_erased += 1
        await self._log.record(
            tenant_id=resolved_tenant,
            run_id=run_id,
            action="crypto_erase_audits",
            reason=reason,
            detail={"count": result.audits_erased},
        )

        # 2. Delete checkpoints (richest plaintext snapshot).
        result.checkpoints_deleted = await self._runs.erase_checkpoints_for_run(run_id)
        await self._log.record(
            tenant_id=resolved_tenant,
            run_id=run_id,
            action="erase_checkpoints",
            reason=reason,
            detail={"count": result.checkpoints_deleted},
        )

        # 3. Redact the runs row (keep it for continuity).
        result.run_redacted = await self._runs.redact_run(run_id)
        await self._log.record(
            tenant_id=resolved_tenant,
            run_id=run_id,
            action="redact_run",
            reason=reason,
            detail={"redacted": result.run_redacted},
        )

        # 4. Artifact cleanup: run-prefix sweep + per-key delete of references.
        result.artifacts_deleted = await self._cleanup_artifacts(run_id, artifact_keys)
        await self._log.record(
            tenant_id=resolved_tenant,
            run_id=run_id,
            action="artifact_cleanup",
            reason=reason,
            detail={"count": result.artifacts_deleted},
        )

        # 5. Econ events (optional hook; best-effort join keys).
        result.econ_events_deleted = await self._erase_econ_events(
            resolved_tenant, run_id, sorted(join_keys), reason
        )

        await self._log.record(
            tenant_id=resolved_tenant,
            run_id=run_id,
            action="erase_run_complete",
            reason=reason,
            detail={
                "audits_erased": result.audits_erased,
                "checkpoints_deleted": result.checkpoints_deleted,
                "run_redacted": result.run_redacted,
                "artifacts_deleted": result.artifacts_deleted,
                "econ_events_deleted": result.econ_events_deleted,
            },
        )
        return result

    async def purge_tenant(self, tenant_id: str) -> list[ErasureResult]:
        """TTL purge: erase every aged, non-held run for a tenant.

        Resolves the tenant's retention policy, computes the audit TTL cutoff, and
        erases each run with records older than the cutoff — skipping runs frozen
        by a legal hold. A ``None`` audit TTL (keep forever) or a tenant-wide hold
        means nothing is purged.
        """
        policy = await self._policies.resolve(tenant_id)
        if not policy.enabled or policy.audit_ttl_seconds is None:
            return []

        holds = await self._holds.active_holds_for_tenant(tenant_id)
        if holds.tenant_wide:
            await self._log.record(
                tenant_id=tenant_id,
                action="purge_skipped_tenant_hold",
                reason="ttl",
            )
            return []

        cutoff = datetime.now(UTC) - timedelta(seconds=policy.audit_ttl_seconds)
        aged = await self._audits.list_erasable(
            tenant_id, cutoff, exclude_run_ids=holds.run_ids
        )
        run_ids = list(dict.fromkeys(record.run_id for record in aged))

        results: list[ErasureResult] = []
        for run_id in run_ids:
            try:
                results.append(
                    await self.erase_run(run_id, "ttl", tenant_id=tenant_id)
                )
            except LegalHoldError:
                continue  # raced with a hold placed mid-sweep; leave it frozen
        return results

    async def _resolve_tenant(
        self,
        run_id: str,
        tenant_id: str | None,
        records: Sequence[Any],
    ) -> str:
        if tenant_id is not None:
            return tenant_id
        run = await self._runs.get(run_id)
        if run is not None:
            return run.tenant_id
        if records:
            return records[0].tenant_id
        return "default"

    async def _cleanup_artifacts(self, run_id: str, keys: set[str]) -> int:
        if self._artifact_store is None:
            return 0
        deleted = 0
        try:
            cleanup = self._artifact_store.cleanup_run
        except AttributeError:
            cleanup = None
        if cleanup is not None:
            try:
                deleted += int(await cleanup(run_id) or 0)
            except Exception:
                logger.exception("artifact cleanup_run failed for run %s", run_id)
        for key in sorted(keys):
            try:
                if await self._artifact_store.delete(key):
                    deleted += 1
            except Exception:
                logger.exception("artifact delete failed for key %s", key)
        return deleted

    async def _erase_econ_events(
        self,
        tenant_id: str,
        run_id: str,
        join_keys: list[str],
        reason: str,
    ) -> int | None:
        if self._econ_eraser is None:
            await self._log.record(
                tenant_id=tenant_id,
                run_id=run_id,
                action="econ_erase_skipped",
                reason=reason,
                detail={"note": "econ eraser not wired (see docs)"},
            )
            return None
        try:
            deleted = await self._econ_eraser.delete_events_for_run(join_keys)
        except Exception:
            logger.exception("econ event erasure failed for run %s", run_id)
            await self._log.record(
                tenant_id=tenant_id,
                run_id=run_id,
                action="econ_erase_failed",
                reason=reason,
            )
            return None
        await self._log.record(
            tenant_id=tenant_id,
            run_id=run_id,
            action="econ_erase",
            reason=reason,
            detail={"count": deleted, "join_keys": join_keys},
        )
        return deleted

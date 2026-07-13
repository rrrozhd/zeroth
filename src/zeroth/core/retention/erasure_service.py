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
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zeroth.core.retention.coordination import RetentionCoordinator
from zeroth.core.retention.models import ErasureResult
from zeroth.core.storage.json import from_json_value

if TYPE_CHECKING:
    from zeroth.core.audit.repository import AuditRepository
    from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
    from zeroth.core.retention.econ_eraser import EconEventEraser
    from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository
    from zeroth.core.runs.repository import RunRepository

logger = logging.getLogger(__name__)

_AUDIT_CLEANUP_PAYLOAD_FIELDS = (
    "input_snapshot",
    "output_snapshot",
    "validation_results",
    "execution_metadata",
    "condition_results",
    "memory_interactions",
    "tool_calls",
    "approval_actions",
)


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


def _harvest_join_keys(payload: Any) -> set[str]:
    """Recursively collect explicit economic ``join_key`` values."""
    found: set[str] = set()
    if isinstance(payload, dict):
        join_key = payload.get("join_key")
        if isinstance(join_key, str) and join_key:
            found.add(join_key)
        for value in payload.values():
            found |= _harvest_join_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= _harvest_join_keys(item)
    return found


def _audit_cleanup_payloads(record: Any) -> tuple[Any, ...]:
    """Return every structured audit surface that can hold cleanup references."""
    dumped = record.model_dump(mode="json")
    return tuple(dumped[field] for field in _AUDIT_CLEANUP_PAYLOAD_FIELDS)


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
        self._coordinator = RetentionCoordinator(run_repository.database)

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
        initial_records = await self._audits.list_by_run(run_id)
        resolved_tenant = await self._resolve_tenant(run_id, tenant_id, initial_records)
        result = ErasureResult(run_id=run_id, tenant_id=resolved_tenant, reason=reason)
        blocked = False
        authorization_log_id = ""
        manifest: dict[str, Any] = {}

        # The legal-hold decision, plaintext harvest, database erasure, and
        # authorization evidence are one tenant-serialized database transaction.
        async with self._coordinator.transaction(resolved_tenant) as transaction:
            await self._after_lock_acquired()
            holds = await self._holds.active_holds_for_tenant_in_transaction(transaction)
            if holds.blocks(run_id):
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=resolved_tenant,
                    run_id=run_id,
                    action="erasure_refused_legal_hold",
                    reason=reason,
                )
                blocked = True
            else:
                persisted_tenant = await self._runs.tenant_id_for_run_in_transaction(
                    transaction.connection,
                    run_id,
                )
                if persisted_tenant is not None and persisted_tenant != resolved_tenant:
                    raise ValueError(
                        f"run {run_id!r} does not belong to tenant {resolved_tenant!r}"
                    )
                records = await self._audits.list_by_run_in_transaction(
                    transaction.connection,
                    run_id,
                )
                payloads: list[Any] = []
                for record in records:
                    if record.tenant_id != resolved_tenant:
                        raise ValueError(
                            f"run {run_id!r} does not belong to tenant {resolved_tenant!r}"
                        )
                    payloads.extend(_audit_cleanup_payloads(record))
                payloads.extend(
                    await self._runs.erasure_payloads_in_transaction(
                        transaction.connection,
                        run_id,
                    )
                )
                artifact_keys = sorted(
                    set().union(*(_harvest_artifact_keys(payload) for payload in payloads))
                    if payloads
                    else set()
                )
                join_keys = sorted(
                    {run_id}.union(*(_harvest_join_keys(payload) for payload in payloads))
                )

                for record in records:
                    if (record.digest_version or 1) < 2:
                        logger.info(
                            "skipping legacy (digest_version=1) audit %s during erasure of run %s",
                            record.audit_id,
                            run_id,
                        )
                        continue
                    was_erased = record.erased
                    erased = await self._audits.crypto_erase_in_transaction(
                        transaction.connection,
                        record.audit_id,
                        reason=reason,
                        record=record,
                    )
                    if erased is not None and not was_erased:
                        result.audits_erased += 1
                result.checkpoints_deleted = (
                    await self._runs.erase_checkpoints_for_run_in_transaction(
                        transaction.connection,
                        run_id,
                    )
                )
                result.run_redacted = await self._runs.redact_run_in_transaction(
                    transaction.connection,
                    run_id,
                )
                manifest = self._new_cleanup_manifest(artifact_keys, join_keys)
                manifest["database_result"] = self._result_detail(result)
                authorization_log_id = await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=resolved_tenant,
                    run_id=run_id,
                    action="erasure_authorized",
                    reason=reason,
                    detail=manifest,
                )

        if blocked:
            raise LegalHoldError(
                f"run {run_id!r} is under an active legal hold and cannot be erased"
            )

        await self._record_database_compatibility_steps(result)
        return await self._perform_external_cleanup(
            authorization_log_id=authorization_log_id,
            manifest=manifest,
            result=result,
        )

    async def retry_external_cleanup(self, log_id: str) -> ErasureResult:
        """Retry an authorization log's unfinished external cleanup operations."""
        row = await self._log.get(log_id)
        if row is None or row["action"] != "erasure_authorized":
            raise ValueError(f"retention authorization log {log_id!r} was not found")
        detail = from_json_value(row["detail"])
        if not isinstance(detail, dict):
            raise ValueError(f"retention authorization log {log_id!r} has no manifest")
        run_id = str(row["run_id"])
        tenant_id = str(row["tenant_id"])
        reason = str(row["reason"])
        manifest = deepcopy(detail)
        entries = await self._log.list_for_run(run_id)
        latest_terminal_action: str | None = None
        for entry in entries:
            if entry["action"] not in {
                "external_cleanup_completed",
                "external_cleanup_failed",
            }:
                continue
            progress = from_json_value(entry["detail"])
            if not isinstance(progress, dict) or progress.get("authorization_log_id") != log_id:
                continue
            manifest["cleanup_status"] = progress["cleanup_status"]
            latest_terminal_action = str(entry["action"])
        result_data = manifest.get("database_result", {})
        result = ErasureResult(
            run_id=run_id,
            tenant_id=tenant_id,
            reason=reason,
            audits_erased=int(result_data.get("audits_erased", 0)),
            checkpoints_deleted=int(result_data.get("checkpoints_deleted", 0)),
            run_redacted=bool(result_data.get("run_redacted", False)),
        )
        if latest_terminal_action == "external_cleanup_completed":
            return self._apply_external_counts(result, manifest["cleanup_status"])
        return await self._perform_external_cleanup(
            authorization_log_id=log_id,
            manifest=manifest,
            result=result,
        )

    async def _after_lock_acquired(self) -> None:
        """Test seam invoked while the tenant coordination lock is held."""

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
        aged = await self._audits.list_erasable(tenant_id, cutoff, exclude_run_ids=holds.run_ids)
        run_ids = list(dict.fromkeys(record.run_id for record in aged))

        results: list[ErasureResult] = []
        for run_id in run_ids:
            try:
                results.append(await self.erase_run(run_id, "ttl", tenant_id=tenant_id))
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

    def _new_cleanup_manifest(
        self,
        artifact_keys: list[str],
        join_keys: list[str],
    ) -> dict[str, Any]:
        artifact_status = "pending" if self._artifact_store is not None else "skipped"
        econ_status = "pending" if self._econ_eraser is not None else "skipped"
        return {
            "artifact_keys": artifact_keys,
            "join_keys": join_keys,
            "cleanup_status": {
                "artifact_prefix": {
                    "status": (
                        artifact_status
                        if getattr(self._artifact_store, "cleanup_run", None) is not None
                        else "skipped"
                    ),
                    "deleted_count": 0,
                },
                "artifact_keys": {
                    key: {"status": artifact_status, "deleted_count": 0} for key in artifact_keys
                },
                "econ": {"status": econ_status, "deleted_count": None},
            },
        }

    async def _perform_external_cleanup(
        self,
        *,
        authorization_log_id: str,
        manifest: dict[str, Any],
        result: ErasureResult,
    ) -> ErasureResult:
        status = deepcopy(manifest["cleanup_status"])
        prefix = status["artifact_prefix"]
        if prefix["status"] != "completed" and prefix["status"] != "skipped":
            try:
                prefix["deleted_count"] = int(
                    await self._artifact_store.cleanup_run(result.run_id) or 0
                )
                prefix["status"] = "completed"
                prefix.pop("error", None)
            except Exception as exc:
                logger.exception("artifact cleanup_run failed for run %s", result.run_id)
                prefix.update(status="failed", error=str(exc))

        for key in manifest["artifact_keys"]:
            key_status = status["artifact_keys"][key]
            if key_status["status"] in {"completed", "skipped"}:
                continue
            try:
                key_status["deleted_count"] = int(bool(await self._artifact_store.delete(key)))
                key_status["status"] = "completed"
                key_status.pop("error", None)
            except Exception as exc:
                logger.exception("artifact delete failed for key %s", key)
                key_status.update(status="failed", error=str(exc))

        econ = status["econ"]
        if econ["status"] not in {"completed", "skipped"}:
            try:
                econ["deleted_count"] = int(
                    await self._econ_eraser.delete_events_for_run(manifest["join_keys"])
                )
                econ["status"] = "completed"
                econ.pop("error", None)
            except Exception as exc:
                logger.exception("econ event erasure failed for run %s", result.run_id)
                econ.update(status="failed", error=str(exc))

        manifest["cleanup_status"] = status
        failed = (
            prefix["status"] == "failed"
            or econ["status"] == "failed"
            or any(item["status"] == "failed" for item in status["artifact_keys"].values())
        )
        await self._log.record(
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            action=("external_cleanup_failed" if failed else "external_cleanup_completed"),
            reason=result.reason,
            detail={
                "authorization_log_id": authorization_log_id,
                "cleanup_status": status,
            },
        )
        self._apply_external_counts(result, status)
        await self._record_external_compatibility_steps(result, manifest, failed=failed)
        return result

    @staticmethod
    def _apply_external_counts(result: ErasureResult, status: dict[str, Any]) -> ErasureResult:
        result.artifacts_deleted = int(status["artifact_prefix"].get("deleted_count") or 0)
        result.artifacts_deleted += sum(
            int(item.get("deleted_count") or 0) for item in status["artifact_keys"].values()
        )
        result.econ_events_deleted = (
            None if status["econ"]["status"] == "skipped" else status["econ"].get("deleted_count")
        )
        return result

    async def _record_database_compatibility_steps(self, result: ErasureResult) -> None:
        for action, detail in (
            ("crypto_erase_audits", {"count": result.audits_erased}),
            ("erase_checkpoints", {"count": result.checkpoints_deleted}),
            ("redact_run", {"redacted": result.run_redacted}),
        ):
            await self._log.record(
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                action=action,
                reason=result.reason,
                detail=detail,
            )

    async def _record_external_compatibility_steps(
        self,
        result: ErasureResult,
        manifest: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        await self._log.record(
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            action="artifact_cleanup",
            reason=result.reason,
            detail={"count": result.artifacts_deleted},
        )
        econ_action = "econ_erase_skipped"
        if manifest["cleanup_status"]["econ"]["status"] == "completed":
            econ_action = "econ_erase"
        elif manifest["cleanup_status"]["econ"]["status"] == "failed":
            econ_action = "econ_erase_failed"
        await self._log.record(
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            action=econ_action,
            reason=result.reason,
            detail={
                "count": result.econ_events_deleted,
                "join_keys": manifest["join_keys"],
            },
        )
        if not failed:
            await self._log.record(
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                action="erase_run_complete",
                reason=result.reason,
                detail=self._result_detail(result),
            )

    @staticmethod
    def _result_detail(result: ErasureResult) -> dict[str, Any]:
        return {
            "audits_erased": result.audits_erased,
            "checkpoints_deleted": result.checkpoints_deleted,
            "run_redacted": result.run_redacted,
            "artifacts_deleted": result.artifacts_deleted,
            "econ_events_deleted": result.econ_events_deleted,
        }

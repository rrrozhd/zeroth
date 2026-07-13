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

import inspect
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from zeroth.core.artifacts.helpers import extract_artifact_refs
from zeroth.core.audit.erasure_schema import AUDIT_CLEANUP_PAYLOAD_FIELDS
from zeroth.core.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
    parse_cleanup_manifest,
)
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


class LegalHoldError(RuntimeError):
    """Raised when erasure is refused because an active legal hold covers it."""


def _harvest_artifact_keys(payload: Any, *, run_id: str) -> set[str]:
    """Collect fully validated artifact refs owned by ``run_id``'s key namespace."""
    refs = extract_artifact_refs({"payload": payload})
    prefix = f"{run_id}/"
    return {ref.key for ref in refs if ref.key.startswith(prefix)}


def _audit_cleanup_payloads(record: Any) -> tuple[Any, ...]:
    """Return every structured audit surface that can hold cleanup references."""
    dumped = record.model_dump(mode="json")
    return tuple(dumped[field] for field in AUDIT_CLEANUP_PAYLOAD_FIELDS)


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
        manifest: CleanupManifest | None = None

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
                join_keys: set[str] = {run_id}
                for record in records:
                    if record.tenant_id != resolved_tenant:
                        raise ValueError(
                            f"run {run_id!r} does not belong to tenant {resolved_tenant!r}"
                        )
                    payloads.extend(_audit_cleanup_payloads(record))
                    authoritative_join_key = record.execution_metadata.get("join_key")
                    if isinstance(authoritative_join_key, str) and authoritative_join_key:
                        join_keys.add(authoritative_join_key)
                payloads.extend(
                    await self._runs.erasure_payloads_in_transaction(
                        transaction.connection,
                        run_id,
                    )
                )
                artifact_keys = sorted(
                    set().union(
                        *(_harvest_artifact_keys(payload, run_id=run_id) for payload in payloads)
                    )
                    if payloads
                    else set()
                )
                sorted_join_keys = sorted(join_keys)

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
                manifest = self._new_cleanup_manifest(
                    result,
                    artifact_keys,
                    sorted_join_keys,
                )
                authorization_log_id = await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=resolved_tenant,
                    run_id=run_id,
                    action="erasure_authorized",
                    reason=reason,
                    detail=manifest.model_dump(mode="json"),
                )

        if blocked:
            raise LegalHoldError(
                f"run {run_id!r} is under an active legal hold and cannot be erased"
            )

        result = await self.retry_external_cleanup(authorization_log_id)
        await self._record_database_compatibility_steps(result)
        return result

    async def retry_external_cleanup(self, log_id: str) -> ErasureResult:
        """Retry an authorization log's unfinished external cleanup operations."""
        row = await self._log.get(log_id)
        if row is None or row["action"] != "erasure_authorized":
            raise ValueError(f"retention authorization log {log_id!r} was not found")
        run_id = str(row["run_id"])
        tenant_id = str(row["tenant_id"])
        reason = str(row["reason"])
        claim_id = uuid4().hex
        claim_log_id: str | None = None
        manifest: CleanupManifest
        async with self._coordinator.transaction(tenant_id) as transaction:
            locked_row = await self._log.get_in_transaction(transaction.connection, log_id)
            if locked_row is None or locked_row["action"] != "erasure_authorized":
                raise ValueError(f"retention authorization log {log_id!r} was not found")
            entries = await self._log.list_for_run_in_transaction(
                transaction.connection,
                run_id,
            )
            manifest = self._manifest_from_log_entries(locked_row, entries)
            active_claim_log_id = self._active_claim_log_id(entries, log_id)
            if active_claim_log_id is not None:
                return self._result_from_manifest(
                    manifest,
                    authorization_log_id=log_id,
                    retry_log_id=active_claim_log_id,
                    force_status="pending",
                )
            if self._manifest_complete(manifest):
                return self._result_from_manifest(
                    manifest,
                    authorization_log_id=log_id,
                    retry_log_id=None,
                )
            claim_log_id = await self._log.record_in_transaction(
                transaction.connection,
                tenant_id=tenant_id,
                run_id=run_id,
                action="external_cleanup_claimed",
                reason=reason,
                detail={
                    "authorization_log_id": log_id,
                    "claim_id": claim_id,
                    "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                },
            )

        try:
            terminal_log_id = await self._execute_claimed_cleanup(
                authorization_log_id=log_id,
                claim_id=claim_id,
                manifest=manifest,
            )
        except BaseException:
            await self._release_cleanup_claim(
                authorization_log_id=log_id,
                claim_id=claim_id,
                tenant_id=tenant_id,
                run_id=run_id,
                reason=reason,
            )
            raise
        return self._result_from_manifest(
            manifest,
            authorization_log_id=log_id,
            retry_log_id=terminal_log_id or claim_log_id,
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
        result: ErasureResult,
        artifact_keys: list[str],
        join_keys: list[str],
    ) -> CleanupManifest:
        artifact_status = "pending" if self._artifact_store is not None else "skipped"
        econ_status = "pending" if self._econ_eraser is not None else "skipped"
        operations = [
            CleanupOperation(
                operation_id=operation_id(
                    result.tenant_id, result.run_id, "artifact_prefix", result.run_id
                ),
                kind="artifact_prefix",
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                status=(
                    artifact_status
                    if getattr(self._artifact_store, "cleanup_run", None) is not None
                    else "skipped"
                ),
            )
        ]
        operations.extend(
            CleanupOperation(
                operation_id=operation_id(result.tenant_id, result.run_id, "artifact_key", key),
                kind="artifact_key",
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                artifact_key=key,
                status=artifact_status,
            )
            for key in artifact_keys
        )
        operations.append(
            CleanupOperation(
                operation_id=operation_id(
                    result.tenant_id,
                    result.run_id,
                    "econ",
                    "\0".join(join_keys),
                ),
                kind="econ",
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                join_keys=join_keys,
                status=econ_status,
                deleted_count=None,
            )
        )
        return CleanupManifest(
            tenant_id=result.tenant_id,
            run_id=result.run_id,
            reason=result.reason,
            database_result=DatabaseErasureOutcome(
                audits_erased=result.audits_erased,
                checkpoints_deleted=result.checkpoints_deleted,
                run_redacted=result.run_redacted,
            ),
            operations=operations,
        )

    def _manifest_from_log_entries(
        self,
        authorization: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> CleanupManifest:
        log_id = str(authorization["log_id"])
        tenant_id = str(authorization["tenant_id"])
        run_id = str(authorization["run_id"])
        reason = str(authorization["reason"])
        authorization_detail = from_json_value(authorization["detail"])
        manifest = parse_cleanup_manifest(
            authorization_detail,
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
        )
        for entry in entries:
            if entry["action"] not in {
                "external_cleanup_operation",
                "external_cleanup_completed",
                "external_cleanup_failed",
            }:
                continue
            detail = from_json_value(entry["detail"])
            if not isinstance(detail, dict) or detail.get("authorization_log_id") != log_id:
                continue
            if "manifest" in detail:
                manifest = parse_cleanup_manifest(
                    detail["manifest"],
                    tenant_id=tenant_id,
                    run_id=run_id,
                    reason=reason,
                )
            elif (
                isinstance(authorization_detail, dict)
                and "cleanup_status" in detail
                and "version" not in authorization_detail
            ):
                migrated_detail = dict(authorization_detail)
                migrated_detail["cleanup_status"] = detail["cleanup_status"]
                manifest = parse_cleanup_manifest(
                    migrated_detail,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    reason=reason,
                )
        return manifest

    @staticmethod
    def _active_claim_log_id(
        entries: list[dict[str, Any]],
        log_id: str,
    ) -> str | None:
        active_claim: tuple[str, dict[str, Any]] | None = None
        for entry in entries:
            detail = from_json_value(entry["detail"])
            if not isinstance(detail, dict) or detail.get("authorization_log_id") != log_id:
                continue
            if entry["action"] == "external_cleanup_claimed":
                active_claim = (str(entry["log_id"]), detail)
            elif entry["action"] in {
                "external_cleanup_claim_released",
                "external_cleanup_completed",
                "external_cleanup_failed",
            }:
                active_claim = None
        if active_claim is None:
            return None
        claim_log_id, claim_detail = active_claim
        try:
            expires = datetime.fromisoformat(str(claim_detail["lease_expires_at"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid external cleanup claim") from exc
        return claim_log_id if expires > datetime.now(UTC) else None

    @staticmethod
    def _manifest_complete(manifest: CleanupManifest) -> bool:
        return all(
            operation.status in {"completed", "skipped"} for operation in manifest.operations
        )

    async def _execute_claimed_cleanup(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        manifest: CleanupManifest,
    ) -> str:
        terminal_log_id = ""
        for index, operation in enumerate(manifest.operations):
            if operation.status in {"completed", "skipped"}:
                continue
            manifest.operations[index] = operation.model_copy(
                update={"status": "in_progress", "error": None}
            )
            await self._record_manifest_progress(
                authorization_log_id,
                claim_id,
                manifest,
            )
            try:
                deleted = await self._execute_operation(manifest.operations[index])
            except Exception as exc:
                logger.exception("external cleanup operation %s failed", operation.operation_id)
                manifest.operations[index] = manifest.operations[index].model_copy(
                    update={"status": "failed", "error": str(exc)}
                )
            else:
                manifest.operations[index] = manifest.operations[index].model_copy(
                    update={"status": "completed", "deleted_count": deleted, "error": None}
                )
            await self._record_manifest_progress(
                authorization_log_id,
                claim_id,
                manifest,
            )
        failed = any(operation.status == "failed" for operation in manifest.operations)
        terminal_log_id = await self._log.record(
            tenant_id=manifest.tenant_id,
            run_id=manifest.run_id,
            action=("external_cleanup_failed" if failed else "external_cleanup_completed"),
            reason=manifest.reason,
            detail={
                "authorization_log_id": authorization_log_id,
                "claim_id": claim_id,
                "manifest": manifest.model_dump(mode="json"),
            },
        )
        result = self._result_from_manifest(
            manifest,
            authorization_log_id=authorization_log_id,
            retry_log_id=terminal_log_id,
        )
        await self._record_external_compatibility_steps(result, manifest, failed=failed)
        return terminal_log_id

    async def _execute_operation(self, operation: CleanupOperation) -> int:
        if operation.kind == "artifact_prefix":
            return int(
                await self._call_with_idempotency(
                    self._artifact_store.cleanup_run,
                    operation.run_id,
                    idempotency_key=operation.operation_id,
                )
                or 0
            )
        if operation.kind == "artifact_key":
            return int(
                bool(
                    await self._call_with_idempotency(
                        self._artifact_store.delete,
                        operation.artifact_key,
                        idempotency_key=operation.operation_id,
                    )
                )
            )
        return int(
            await self._econ_eraser.delete_events_for_run(
                operation.tenant_id,
                operation.join_keys,
                idempotency_key=operation.operation_id,
            )
        )

    @staticmethod
    async def _call_with_idempotency(
        method: Any,
        *args: Any,
        idempotency_key: str,
    ) -> Any:
        if "idempotency_key" in inspect.signature(method).parameters:
            return await method(*args, idempotency_key=idempotency_key)
        return await method(*args)

    async def _record_manifest_progress(
        self,
        authorization_log_id: str,
        claim_id: str,
        manifest: CleanupManifest,
    ) -> str:
        return await self._log.record(
            tenant_id=manifest.tenant_id,
            run_id=manifest.run_id,
            action="external_cleanup_operation",
            reason=manifest.reason,
            detail={
                "authorization_log_id": authorization_log_id,
                "claim_id": claim_id,
                "manifest": manifest.model_dump(mode="json"),
            },
        )

    async def _release_cleanup_claim(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        tenant_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        try:
            async with self._coordinator.transaction(tenant_id) as transaction:
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    action="external_cleanup_claim_released",
                    reason=reason,
                    detail={
                        "authorization_log_id": authorization_log_id,
                        "claim_id": claim_id,
                    },
                )
        except Exception:
            logger.exception("failed to release external cleanup claim %s", claim_id)

    @staticmethod
    def _result_from_manifest(
        manifest: CleanupManifest,
        *,
        authorization_log_id: str,
        retry_log_id: str | None,
        force_status: str | None = None,
    ) -> ErasureResult:
        statuses = {operation.status for operation in manifest.operations}
        cleanup_status = force_status
        if cleanup_status is None:
            if "failed" in statuses:
                cleanup_status = "failed"
            elif statuses <= {"completed", "skipped"}:
                cleanup_status = "complete"
            else:
                cleanup_status = "pending"
        artifact_deleted = sum(
            int(operation.deleted_count or 0)
            for operation in manifest.operations
            if operation.kind in {"artifact_prefix", "artifact_key"}
        )
        econ_operations = [
            operation for operation in manifest.operations if operation.kind == "econ"
        ]
        econ_deleted = None
        if econ_operations and econ_operations[0].status != "skipped":
            econ_deleted = int(econ_operations[0].deleted_count or 0)
        database = manifest.database_result
        return ErasureResult(
            run_id=manifest.run_id,
            tenant_id=manifest.tenant_id,
            reason=manifest.reason,
            audits_erased=database.audits_erased,
            checkpoints_deleted=database.checkpoints_deleted,
            run_redacted=database.run_redacted,
            artifacts_deleted=artifact_deleted,
            econ_events_deleted=econ_deleted,
            external_cleanup_status=cleanup_status,  # type: ignore[arg-type]
            authorization_log_id=authorization_log_id,
            retry_log_id=retry_log_id,
        )

    async def _record_database_compatibility_steps(self, result: ErasureResult) -> None:
        for action, detail in (
            ("crypto_erase_audits", {"count": result.audits_erased}),
            ("erase_checkpoints", {"count": result.checkpoints_deleted}),
            ("redact_run", {"redacted": result.run_redacted}),
        ):
            try:
                await self._log.record(
                    tenant_id=result.tenant_id,
                    run_id=result.run_id,
                    action=action,
                    reason=result.reason,
                    detail=detail,
                )
            except Exception:
                logger.exception("best-effort compatibility log %s failed", action)

    async def _record_external_compatibility_steps(
        self,
        result: ErasureResult,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> None:
        await self._record_compatibility_log(
            result,
            "artifact_cleanup",
            {"count": result.artifacts_deleted},
        )
        econ = next(
            (operation for operation in manifest.operations if operation.kind == "econ"),
            None,
        )
        if econ is None:
            if not failed:
                await self._record_compatibility_log(
                    result,
                    "erase_run_complete",
                    self._result_detail(result),
                )
            return
        econ_action = "econ_erase_skipped"
        if econ.status == "completed":
            econ_action = "econ_erase"
        elif econ.status == "failed":
            econ_action = "econ_erase_failed"
        await self._record_compatibility_log(
            result,
            econ_action,
            {
                "count": result.econ_events_deleted,
                "join_keys": econ.join_keys,
            },
        )
        if not failed:
            await self._record_compatibility_log(
                result,
                "erase_run_complete",
                self._result_detail(result),
            )

    async def _record_compatibility_log(
        self,
        result: ErasureResult,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        try:
            await self._log.record(
                tenant_id=result.tenant_id,
                run_id=result.run_id,
                action=action,
                reason=result.reason,
                detail=detail,
            )
        except Exception:
            logger.exception("best-effort compatibility log %s failed", action)

    @staticmethod
    def _result_detail(result: ErasureResult) -> dict[str, Any]:
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

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

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
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
from zeroth.core.retention.cleanup_state_repository import (
    CleanupStateRecord,
    CleanupStateRepository,
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


class StaleCleanupClaimError(RuntimeError):
    """Raised when an expired cleanup worker attempts to mutate newer state."""


@dataclass(slots=True)
class _CleanupReplayState:
    manifest: CleanupManifest
    generation: int = 0
    revision: int = 0
    active_claim_id: str | None = None
    active_claim_log_id: str | None = None
    lease_expires_at: datetime | None = None
    terminal_status: str | None = None
    terminal_log_id: str | None = None


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
        cleanup_lease_seconds: float = 30.0,
    ) -> None:
        self._audits = audit_repository
        self._runs = run_repository
        self._policies = policy_repository
        self._holds = legal_hold_repository
        self._log = log_repository
        self._artifact_store = artifact_store
        self._econ_eraser = econ_eraser
        self._coordinator = RetentionCoordinator(run_repository.database)
        self._cleanup_state = CleanupStateRepository()
        self._cleanup_lease_seconds = cleanup_lease_seconds

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
                await self._cleanup_state.initialize_in_transaction(
                    transaction.connection,
                    authorization_log_id=authorization_log_id,
                    manifest=manifest,
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
        generation = 0
        manifest: CleanupManifest
        async with self._coordinator.transaction(tenant_id) as transaction:
            locked_row = await self._log.get_in_transaction(transaction.connection, log_id)
            if locked_row is None or locked_row["action"] != "erasure_authorized":
                raise ValueError(f"retention authorization log {log_id!r} was not found")
            state = await self._load_or_materialize_cleanup_state(
                transaction.connection,
                locked_row,
            )
            manifest = state.manifest
            if (
                state.active_claim_id is not None
                and state.lease_expires_at is not None
                and state.lease_expires_at > datetime.now(UTC)
            ):
                return self._result_from_manifest(
                    manifest,
                    authorization_log_id=log_id,
                    retry_log_id=state.active_claim_log_id,
                    force_status="pending",
                )
            if self._manifest_complete(manifest):
                terminal_log_id = state.terminal_log_id
                if terminal_log_id is None:
                    terminal_log_id = await self._log.record_in_transaction(
                        transaction.connection,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        action="external_cleanup_completed",
                        reason=reason,
                        detail={
                            "authorization_log_id": log_id,
                            "claim_id": None,
                            "generation": state.generation,
                            "revision": state.revision + 1,
                            "repaired": True,
                        },
                    )
                    await self._cleanup_state.repair_terminal_in_transaction(
                        transaction.connection,
                        authorization_log_id=log_id,
                        generation=state.generation,
                        expected_revision=state.revision,
                        terminal_log_id=terminal_log_id,
                    )
                return self._result_from_manifest(
                    manifest,
                    authorization_log_id=log_id,
                    retry_log_id=terminal_log_id,
                )
            generation = state.generation + 1
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self._cleanup_lease_seconds)
            claim_log_id = await self._log.record_in_transaction(
                transaction.connection,
                tenant_id=tenant_id,
                run_id=run_id,
                action="external_cleanup_claimed",
                reason=reason,
                detail={
                    "authorization_log_id": log_id,
                    "claim_id": claim_id,
                    "generation": generation,
                    "revision": state.revision + 1,
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
            )
            await self._cleanup_state.claim_in_transaction(
                transaction.connection,
                authorization_log_id=log_id,
                expected_generation=state.generation,
                expected_revision=state.revision,
                claim_id=claim_id,
                claim_log_id=claim_log_id,
                lease_expires_at=lease_expires_at,
            )

        try:
            terminal_log_id = await self._execute_claimed_cleanup(
                authorization_log_id=log_id,
                claim_id=claim_id,
                generation=generation,
                manifest=manifest,
            )
        except BaseException:
            await self._release_cleanup_claim(
                authorization_log_id=log_id,
                claim_id=claim_id,
                generation=generation,
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

    def _replay_cleanup_state(
        self,
        authorization: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> _CleanupReplayState:
        log_id = str(authorization["log_id"])
        tenant_id = str(authorization["tenant_id"])
        run_id = str(authorization["run_id"])
        reason = str(authorization["reason"])
        authorization_detail = from_json_value(authorization["detail"])
        state = _CleanupReplayState(
            manifest=parse_cleanup_manifest(
                authorization_detail,
                tenant_id=tenant_id,
                run_id=run_id,
                reason=reason,
            )
        )
        versioned_events: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for entry in entries:
            detail = from_json_value(entry["detail"])
            if not isinstance(detail, dict) or detail.get("authorization_log_id") != log_id:
                continue
            revision = detail.get("revision")
            generation = detail.get("generation")
            if isinstance(revision, int) and isinstance(generation, int):
                versioned_events.append((revision, entry, detail))
            elif "manifest" in detail:
                state.manifest = parse_cleanup_manifest(
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
                state.manifest = parse_cleanup_manifest(
                    migrated_detail,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    reason=reason,
                )
        operation_indexes = {
            operation.operation_id: index
            for index, operation in enumerate(state.manifest.operations)
        }
        for revision, entry, detail in sorted(versioned_events, key=lambda item: item[0]):
            if revision <= state.revision:
                continue
            generation = int(detail["generation"])
            action = str(entry["action"])
            claim_id = detail.get("claim_id")
            if action == "external_cleanup_claimed":
                if generation <= state.generation:
                    continue
                state.generation = generation
                state.revision = revision
                state.active_claim_id = str(claim_id)
                state.active_claim_log_id = str(entry["log_id"])
                state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
                state.terminal_log_id = None
                continue
            if generation != state.generation:
                continue
            if action == "external_cleanup_heartbeat":
                if claim_id != state.active_claim_id:
                    continue
                state.revision = revision
                state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
            elif action == "external_cleanup_operation":
                if claim_id != state.active_claim_id:
                    continue
                operation_id_value = str(detail["operation_id"])
                index = operation_indexes.get(operation_id_value)
                if index is None:
                    raise ValueError("cleanup delta references unknown operation")
                operation = state.manifest.operations[index]
                state.manifest.operations[index] = operation.model_copy(
                    update={
                        "status": detail["status"],
                        "deleted_count": detail.get("deleted_count"),
                        "error": detail.get("error"),
                    }
                )
                state.revision = revision
                if detail.get("lease_expires_at"):
                    state.lease_expires_at = datetime.fromisoformat(str(detail["lease_expires_at"]))
            elif action == "external_cleanup_claim_released":
                if claim_id != state.active_claim_id:
                    continue
                state.revision = revision
                state.active_claim_id = None
                state.active_claim_log_id = None
                state.lease_expires_at = None
            elif action in {"external_cleanup_completed", "external_cleanup_failed"}:
                if claim_id is not None and claim_id != state.active_claim_id:
                    continue
                state.revision = revision
                state.active_claim_id = None
                state.active_claim_log_id = None
                state.lease_expires_at = None
                state.terminal_status = (
                    "completed" if action == "external_cleanup_completed" else "failed"
                )
                state.terminal_log_id = str(entry["log_id"])
        return state

    async def _load_or_materialize_cleanup_state(
        self,
        connection: Any,
        authorization: dict[str, Any],
    ) -> _CleanupReplayState:
        """Load current state in O(N), replaying audit history once only for legacy rows."""
        authorization_log_id = str(authorization["log_id"])
        materialized = await self._cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if materialized is None:
            entries = await self._log.list_for_run_in_transaction(
                connection,
                str(authorization["run_id"]),
            )
            replayed = self._replay_cleanup_state(authorization, entries)
            await self._cleanup_state.initialize_in_transaction(
                connection,
                authorization_log_id=authorization_log_id,
                manifest=replayed.manifest,
                generation=replayed.generation,
                revision=replayed.revision,
                active_claim_id=replayed.active_claim_id,
                active_claim_log_id=replayed.active_claim_log_id,
                lease_expires_at=replayed.lease_expires_at,
                terminal_status=replayed.terminal_status,
                terminal_log_id=replayed.terminal_log_id,
            )
            return replayed

        tenant_id = str(authorization["tenant_id"])
        run_id = str(authorization["run_id"])
        reason = str(authorization["reason"])
        if (
            materialized.tenant_id != tenant_id
            or materialized.run_id != run_id
            or materialized.reason != reason
        ):
            raise ValueError("cleanup state identity does not match authorization log")
        manifest = parse_cleanup_manifest(
            from_json_value(authorization["detail"]),
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
        )
        operation_rows = await self._cleanup_state.list_operations_in_transaction(
            connection,
            authorization_log_id,
        )
        operation_state = {row.operation_id: row for row in operation_rows}
        manifest_ids = {operation.operation_id for operation in manifest.operations}
        if set(operation_state) != manifest_ids:
            raise ValueError("cleanup operation state does not match authorization manifest")
        manifest.operations = [
            operation.model_copy(
                update={
                    "status": operation_state[operation.operation_id].status,
                    "deleted_count": operation_state[operation.operation_id].deleted_count,
                    "error": operation_state[operation.operation_id].error,
                }
            )
            for operation in manifest.operations
        ]
        return _CleanupReplayState(
            manifest=manifest,
            generation=materialized.generation,
            revision=materialized.revision,
            active_claim_id=materialized.active_claim_id,
            active_claim_log_id=materialized.active_claim_log_id,
            lease_expires_at=materialized.lease_expires_at,
            terminal_status=materialized.terminal_status,
            terminal_log_id=materialized.terminal_log_id,
        )

    async def _get_or_materialize_state_record(
        self,
        connection: Any,
        authorization_log_id: str,
    ) -> CleanupStateRecord:
        state = await self._cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if state is not None:
            return state
        authorization = await self._log.get_in_transaction(connection, authorization_log_id)
        if authorization is None or authorization["action"] != "erasure_authorized":
            raise ValueError("cleanup authorization disappeared")
        await self._load_or_materialize_cleanup_state(connection, authorization)
        state = await self._cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if state is None:  # pragma: no cover - initialization is transactional
            raise RuntimeError("cleanup state initialization failed")
        return state

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
        generation: int,
        manifest: CleanupManifest,
    ) -> str:
        terminal_log_id = ""
        for index, operation in enumerate(manifest.operations):
            if operation.status in {"completed", "skipped"}:
                continue
            manifest.operations[index] = operation.model_copy(
                update={"status": "in_progress", "error": None}
            )
            await self._record_operation_delta(
                authorization_log_id,
                claim_id,
                generation,
                manifest.operations[index],
            )
            try:
                deleted = await self._execute_operation_with_heartbeat(
                    authorization_log_id,
                    claim_id,
                    generation,
                    manifest.operations[index],
                )
            except Exception as exc:
                logger.exception("external cleanup operation %s failed", operation.operation_id)
                manifest.operations[index] = manifest.operations[index].model_copy(
                    update={"status": "failed", "error": str(exc)}
                )
            else:
                manifest.operations[index] = manifest.operations[index].model_copy(
                    update={"status": "completed", "deleted_count": deleted, "error": None}
                )
            await self._record_operation_delta(
                authorization_log_id,
                claim_id,
                generation,
                manifest.operations[index],
            )
        failed = any(operation.status == "failed" for operation in manifest.operations)
        terminal_log_id = await self._record_terminal_fenced(
            authorization_log_id,
            claim_id,
            generation,
            manifest,
            failed=failed,
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

    async def _execute_operation_with_heartbeat(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> int:
        task = asyncio.create_task(self._execute_operation(operation))
        interval = max(self._cleanup_lease_seconds / 3, 0.01)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    return await task
                await self._record_claim_heartbeat(
                    authorization_log_id,
                    claim_id,
                    generation,
                    operation.tenant_id,
                    operation.run_id,
                )
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _record_claim_heartbeat(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        tenant_id: str,
        run_id: str,
    ) -> str:
        async with self._coordinator.transaction(tenant_id) as transaction:
            state = await self._get_or_materialize_state_record(
                transaction.connection,
                authorization_log_id,
            )
            self._verify_active_claim(state, claim_id, generation)
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self._cleanup_lease_seconds)
            log_id = await self._log.record_in_transaction(
                transaction.connection,
                tenant_id=tenant_id,
                run_id=run_id,
                action="external_cleanup_heartbeat",
                reason=state.reason,
                detail={
                    "authorization_log_id": authorization_log_id,
                    "claim_id": claim_id,
                    "generation": generation,
                    "revision": state.revision + 1,
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
            )
            await self._cleanup_state.heartbeat_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                lease_expires_at=lease_expires_at,
            )
            return log_id

    @staticmethod
    async def _call_with_idempotency(
        method: Any,
        *args: Any,
        idempotency_key: str,
    ) -> Any:
        return await method(*args, idempotency_key=idempotency_key)

    async def _record_operation_delta(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> str:
        async with self._coordinator.transaction(operation.tenant_id) as transaction:
            state = await self._get_or_materialize_state_record(
                transaction.connection,
                authorization_log_id,
            )
            self._verify_active_claim(state, claim_id, generation)
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self._cleanup_lease_seconds)
            log_id = await self._log.record_in_transaction(
                transaction.connection,
                tenant_id=operation.tenant_id,
                run_id=operation.run_id,
                action="external_cleanup_operation",
                reason=state.reason,
                detail={
                    "authorization_log_id": authorization_log_id,
                    "claim_id": claim_id,
                    "generation": generation,
                    "revision": state.revision + 1,
                    "lease_expires_at": lease_expires_at.isoformat(),
                    "operation_id": operation.operation_id,
                    "status": operation.status,
                    "deleted_count": operation.deleted_count,
                    "error": operation.error,
                },
            )
            await self._cleanup_state.update_operation_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                operation=operation,
                lease_expires_at=lease_expires_at,
            )
            return log_id

    @staticmethod
    def _verify_active_claim(
        state: _CleanupReplayState,
        claim_id: str,
        generation: int,
    ) -> None:
        if state.active_claim_id != claim_id or state.generation != generation:
            raise StaleCleanupClaimError(
                f"cleanup claim {claim_id!r} generation {generation} is stale"
            )

    async def _record_terminal_fenced(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> str:
        async with self._coordinator.transaction(manifest.tenant_id) as transaction:
            state = await self._get_or_materialize_state_record(
                transaction.connection,
                authorization_log_id,
            )
            self._verify_active_claim(state, claim_id, generation)
            terminal_status = "failed" if failed else "completed"
            log_id = await self._log.record_in_transaction(
                transaction.connection,
                tenant_id=manifest.tenant_id,
                run_id=manifest.run_id,
                action=("external_cleanup_failed" if failed else "external_cleanup_completed"),
                reason=manifest.reason,
                detail={
                    "authorization_log_id": authorization_log_id,
                    "claim_id": claim_id,
                    "generation": generation,
                    "revision": state.revision + 1,
                },
            )
            await self._cleanup_state.terminal_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                terminal_status=terminal_status,
                terminal_log_id=log_id,
            )
            return log_id

    async def _release_cleanup_claim(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        tenant_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        try:
            async with self._coordinator.transaction(tenant_id) as transaction:
                state = await self._get_or_materialize_state_record(
                    transaction.connection,
                    authorization_log_id,
                )
                if state.active_claim_id != claim_id or state.generation != generation:
                    return
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    action="external_cleanup_claim_released",
                    reason=reason,
                    detail={
                        "authorization_log_id": authorization_log_id,
                        "claim_id": claim_id,
                        "generation": generation,
                        "revision": state.revision + 1,
                    },
                )
                await self._cleanup_state.release_in_transaction(
                    transaction.connection,
                    authorization_log_id=authorization_log_id,
                    claim_id=claim_id,
                    generation=generation,
                    expected_revision=state.revision,
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

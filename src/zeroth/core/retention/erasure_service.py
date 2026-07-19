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
from uuid import uuid4

from zeroth.core.audit.erasure_schema import AUDIT_CLEANUP_PAYLOAD_FIELDS
from zeroth.core.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
)
from zeroth.core.retention.cleanup_state_repository import (
    CleanupStateRecord,
    CleanupStateRepository,
)
from zeroth.core.retention.coordination import RetentionCoordinator
from zeroth.core.retention.models import ErasureResult
from zeroth.governance.retention.claims import CleanupClaims
from zeroth.governance.retention.compatibility import CompatibilityLog, result_detail

# Re-exported: both exceptions are protected legacy capabilities recorded at this
# module path. Their definitions moved because the collaborators that raise them
# may not import this facade.
from zeroth.governance.retention.errors import (
    LegalHoldError as LegalHoldError,
)
from zeroth.governance.retention.errors import (
    StaleCleanupClaimError as StaleCleanupClaimError,
)
from zeroth.governance.retention.executor import CleanupExecutor
from zeroth.governance.retention.manifests import (
    build_cleanup_manifest,
    manifest_complete,
    result_from_manifest,
)
from zeroth.governance.retention.replay import CleanupReplayState, replay_cleanup_state
from zeroth.platform.artifacts.helpers import extract_artifact_refs

if TYPE_CHECKING:
    from zeroth.core.audit.repository import AuditRepository
    from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
    from zeroth.core.retention.econ_eraser import EconEventEraser
    from zeroth.core.retention.legal_hold_repository import LegalHoldRepository
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository
    from zeroth.integrations.persistence.runs import RunRepository

logger = logging.getLogger(__name__)


_CleanupReplayState = CleanupReplayState


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

    @property
    def _claims(self) -> CleanupClaims:
        """The claim collaborator, rebuilt per access from this service's own fields.

        Rebuilding rather than storing is deliberate. Tests and callers reassign
        ``_artifact_store``, ``_econ_eraser`` and ``_cleanup_state`` after
        construction, and ``_replay_cleanup_state`` is monkeypatched to count
        legacy materializations. A collaborator captured in ``__init__`` would
        freeze the originals and silently ignore all of it. ``CleanupClaims`` is
        a frozen dataclass, so this costs nothing.
        """
        return CleanupClaims(
            coordinator=self._coordinator,
            log=self._log,
            cleanup_state=self._cleanup_state,
            lease_seconds=self._cleanup_lease_seconds,
            replay=self._replay_cleanup_state,
        )

    @property
    def _compatibility(self) -> CompatibilityLog:
        """The legacy-log collaborator, rebuilt per access (see :attr:`_claims`)."""
        return CompatibilityLog(log=self._log)

    @property
    def _executor(self) -> CleanupExecutor:
        """The external-cleanup collaborator, rebuilt per access (see :attr:`_claims`).

        ``_artifact_store`` and ``_econ_eraser`` are reassigned after construction
        by several callers, so capturing them once would run cleanup against the
        surfaces the service was built with rather than the ones it now has.
        """
        return CleanupExecutor(
            claims=self._claims,
            compatibility=self._compatibility,
            artifact_store=self._artifact_store,
            econ_eraser=self._econ_eraser,
            lease_seconds=self._cleanup_lease_seconds,
        )

    async def erase_run(
        self,
        run_id: str,
        reason: str,
        *,
        tenant_id: str | None = None,
        ttl_cutoff: datetime | None = None,
    ) -> ErasureResult:
        """Erase every PII surface for a run, unless an active legal hold blocks.

        ``reason`` is one of ``ttl`` | ``rte`` | ``manual``. Idempotent: re-running
        deletes nothing already gone and still returns a result. Raises
        :class:`LegalHoldError` when the run (or its tenant) is on hold.

        ``ttl_cutoff`` marks a TTL-sweep call: the run row is locked and its
        tenant/terminal-status/``updated_at`` eligibility re-verified inside the
        destructive transaction, so a run resurrected between unlocked selection
        and erasure is left untouched (an all-zero result is returned).
        """
        initial_records = await self._audits.list_by_run(run_id)
        resolved_tenant = await self._resolve_tenant(run_id, tenant_id, initial_records)
        result = ErasureResult(run_id=run_id, tenant_id=resolved_tenant, reason=reason)
        blocked = False
        ineligible = False
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
            elif ttl_cutoff is not None and (
                await self._runs.lock_and_recheck_erasable_run(
                    transaction.connection,
                    run_id,
                    resolved_tenant,
                    ttl_cutoff,
                )
                is None
            ):
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=resolved_tenant,
                    run_id=run_id,
                    action="ttl_recheck_ineligible",
                    reason=reason,
                )
                ineligible = True
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
        if ineligible:
            return result  # zero surfaces touched; no cleanup manifest exists

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
                    terminal_log_id = await self._claims.repair_terminal(
                        transaction.connection,
                        authorization_log_id=log_id,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        reason=reason,
                        generation=state.generation,
                        revision=state.revision,
                    )
                return self._result_from_manifest(
                    manifest,
                    authorization_log_id=log_id,
                    retry_log_id=terminal_log_id,
                )
            generation = state.generation + 1
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self._cleanup_lease_seconds)
            claim_log_id = await self._claims.claim(
                transaction.connection,
                authorization_log_id=log_id,
                tenant_id=tenant_id,
                run_id=run_id,
                reason=reason,
                claim_id=claim_id,
                generation=generation,
                expected_generation=state.generation,
                expected_revision=state.revision,
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

    async def purge_audits(self, tenant_id: str) -> list[ErasureResult]:
        """Audit-TTL sweep: tombstone aged v2 audits, touching nothing else.

        Crypto-erases each aged audit's PII in one tenant-coordinated
        transaction (holds re-checked inside it) while the run row,
        checkpoints, artifacts, and newer audits remain intact — run-surface
        removal is :meth:`purge_runs`'s job. Returns one result per affected
        run carrying only ``audits_erased``.
        """
        policy = await self._policies.resolve(tenant_id)
        if not policy.enabled or policy.audit_ttl_seconds is None:
            return []
        cutoff = datetime.now(UTC) - timedelta(seconds=policy.audit_ttl_seconds)

        results: dict[str, ErasureResult] = {}
        async with self._coordinator.transaction(tenant_id) as transaction:
            await self._after_lock_acquired()
            holds = await self._holds.active_holds_for_tenant_in_transaction(transaction)
            if holds.tenant_wide:
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=tenant_id,
                    action="purge_skipped_tenant_hold",
                    reason="ttl",
                )
                return []
            records = await self._audits.list_erasable_in_transaction(
                transaction.connection,
                tenant_id,
                cutoff,
                exclude_run_ids=holds.run_ids,
            )
            for record in records:
                was_erased = record.erased
                erased = await self._audits.crypto_erase_in_transaction(
                    transaction.connection,
                    record.audit_id,
                    reason="ttl",
                    record=record,
                )
                if erased is None or was_erased:
                    continue
                result = results.setdefault(
                    record.run_id,
                    ErasureResult(run_id=record.run_id, tenant_id=tenant_id, reason="ttl"),
                )
                result.audits_erased += 1
            for run_id, result in results.items():
                await self._log.record_in_transaction(
                    transaction.connection,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    action="audit_ttl_purged",
                    reason="ttl",
                    detail={"audits_erased": result.audits_erased},
                )
        return list(results.values())

    async def purge_runs(self, tenant_id: str) -> list[ErasureResult]:
        """Run-TTL sweep: fully erase aged COMPLETED/FAILED runs.

        Selection is an unlocked snapshot over persisted ``updated_at`` and
        terminal status; each erasure re-verifies eligibility (and holds)
        inside its own tenant-coordinated destructive transaction, so runs
        resurrected mid-sweep survive untouched.
        """
        policy = await self._policies.resolve(tenant_id)
        if not policy.enabled or policy.run_ttl_seconds is None:
            return []

        holds = await self._holds.active_holds_for_tenant(tenant_id)
        if holds.tenant_wide:
            await self._log.record(
                tenant_id=tenant_id,
                action="purge_skipped_tenant_hold",
                reason="ttl",
            )
            return []

        cutoff = datetime.now(UTC) - timedelta(seconds=policy.run_ttl_seconds)
        run_ids = await self._runs.list_erasable_run_ids(tenant_id, cutoff)

        results: list[ErasureResult] = []
        for run_id in run_ids:
            if run_id in holds.run_ids:
                continue
            try:
                results.append(
                    await self.erase_run(run_id, "ttl", tenant_id=tenant_id, ttl_cutoff=cutoff)
                )
            except LegalHoldError:
                continue  # raced with a hold placed mid-sweep; leave it frozen
        return results

    async def purge_tenant(self, tenant_id: str) -> list[ErasureResult]:
        """Combined TTL sweep: run erasure first, then audit tombstoning.

        Convenience surface for manual/API callers. Run TTL fully erases aged
        terminal runs; audit TTL then tombstones whatever aged audits remain.
        An old audit no longer drags its whole run into erasure — that is
        exclusively the run TTL's decision. The background worker invokes the
        two sweeps independently instead of through this method.
        """
        results = await self.purge_runs(tenant_id)
        results.extend(await self.purge_audits(tenant_id))
        return results

    async def _resolve_tenant(
        self,
        run_id: str,
        tenant_id: str | None,
        records: Sequence[Any],
    ) -> str:
        """Resolve a run's tenant: explicit arg, then run row, then audits, else ``default``."""
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
        """Build the external-cleanup manifest (artifact + econ operations) for an erased run."""
        return build_cleanup_manifest(
            result,
            artifact_keys,
            join_keys,
            artifact_store=self._artifact_store,
            econ_eraser=self._econ_eraser,
        )

    def _replay_cleanup_state(
        self,
        authorization: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> _CleanupReplayState:
        """Rebuild cleanup state by replaying a run's retention audit entries (legacy rows)."""
        return replay_cleanup_state(authorization, entries)

    async def _load_or_materialize_cleanup_state(
        self,
        connection: Any,
        authorization: dict[str, Any],
    ) -> _CleanupReplayState:
        """Load current state in O(N), replaying audit history once only for legacy rows."""
        return await self._claims.load_or_materialize(connection, authorization)

    async def _get_or_materialize_state_record(
        self,
        connection: Any,
        authorization_log_id: str,
    ) -> CleanupStateRecord:
        """Return the materialized cleanup state row, replaying legacy audit history when absent."""
        return await self._claims.state_record(connection, authorization_log_id)

    @staticmethod
    def _manifest_complete(manifest: CleanupManifest) -> bool:
        """Return True when every manifest operation is already completed or skipped."""
        return manifest_complete(manifest)

    async def _execute_claimed_cleanup(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        manifest: CleanupManifest,
    ) -> str:
        """Run all unfinished manifest operations under the claim; return the terminal log id."""
        return await self._executor.execute_claimed(
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            manifest=manifest,
        )

    async def _execute_operation(self, operation: CleanupOperation) -> int:
        """Dispatch one operation to its external surface; return the deleted-item count."""
        return await self._executor.execute_operation(operation)

    async def _execute_operation_with_heartbeat(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> int:
        """Run one operation while heartbeating the claim lease every third of its window."""
        return await self._executor.execute_operation_with_heartbeat(
            authorization_log_id,
            claim_id,
            generation,
            operation,
        )

    async def _record_claim_heartbeat(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        tenant_id: str,
        run_id: str,
    ) -> str:
        """Extend the active claim's lease (fenced against stale claims); return the log id."""
        return await self._claims.record_heartbeat(
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            tenant_id=tenant_id,
            run_id=run_id,
        )

    @staticmethod
    async def _call_with_idempotency(
        method: Any,
        *args: Any,
        idempotency_key: str,
    ) -> Any:
        """Await ``method`` with the operation's idempotency key forwarded."""
        return await CleanupExecutor.call_with_idempotency(
            method, *args, idempotency_key=idempotency_key
        )

    async def _record_operation_delta(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> str:
        """Persist one operation's status delta (log entry + state row) under the claim fence."""
        return await self._claims.record_operation_delta(
            authorization_log_id,
            claim_id,
            generation,
            operation,
        )

    @staticmethod
    def _verify_active_claim(
        state: _CleanupReplayState,
        claim_id: str,
        generation: int,
    ) -> None:
        """Raise :class:`StaleCleanupClaimError` unless the claim/generation still own the state."""
        CleanupClaims.verify_active(state, claim_id, generation)

    async def _record_terminal_fenced(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> str:
        """Record the completed/failed terminal event and state, fenced against stale claims."""
        return await self._claims.record_terminal(
            authorization_log_id,
            claim_id,
            generation,
            manifest,
            failed=failed,
        )

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
        """Best-effort release of an aborted claim so a later retry can re-claim immediately."""
        await self._claims.release(
            authorization_log_id=authorization_log_id,
            claim_id=claim_id,
            generation=generation,
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
        )

    @staticmethod
    def _result_from_manifest(
        manifest: CleanupManifest,
        *,
        authorization_log_id: str,
        retry_log_id: str | None,
        force_status: str | None = None,
    ) -> ErasureResult:
        """Project a cleanup manifest into the :class:`ErasureResult` returned to callers."""
        return result_from_manifest(
            manifest,
            authorization_log_id=authorization_log_id,
            retry_log_id=retry_log_id,
            force_status=force_status,
        )

    async def _record_database_compatibility_steps(self, result: ErasureResult) -> None:
        """Emit legacy per-step database log entries (best-effort; failures only logged)."""
        await self._compatibility.record_database_steps(result)

    async def _record_external_compatibility_steps(
        self,
        result: ErasureResult,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> None:
        """Emit legacy artifact/econ/completion log entries mirroring pre-manifest logging."""
        await self._compatibility.record_external_steps(result, manifest, failed=failed)

    async def _record_compatibility_log(
        self,
        result: ErasureResult,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        """Write one best-effort compatibility log entry; failures are logged, never raised."""
        await self._compatibility.record(result, action, detail)

    @staticmethod
    def _result_detail(result: ErasureResult) -> dict[str, Any]:
        """Serialize an :class:`ErasureResult` into the ``erase_run_complete`` detail payload."""
        return result_detail(result)

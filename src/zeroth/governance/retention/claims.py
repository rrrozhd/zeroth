"""Cleanup claim coordination: leases, fencing, and the CAS writes behind them.

One authorized erasure may be retried by several workers. Exactly one of them
may act at a time, and the winner is decided by a durable claim: a
``(claim_id, generation)`` pair plus a lease that has to be heartbeated to stay
alive. Every mutation re-reads that state inside its own tenant-serialized
transaction and refuses to proceed unless the pair still owns it, so a worker
whose lease expired mid-operation cannot write progress over its successor's.

**Transaction scope is the contract here.** Each writer below opens exactly one
``coordinator.transaction`` and does the state read, the log append, and the CAS
update inside it -- that is what makes the fence atomic. ``load_or_materialize``
and ``state_record`` take a caller-supplied connection instead, because they are
also called from the middle of the service's own transaction; they must never
open one of their own. Splitting either group differently reintroduces the race
this design exists to close.

Replay is injected rather than imported so the service's ``_replay_cleanup_state``
seam stays the single place legacy materialization happens.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zeroth.core.retention.cleanup_manifest import CleanupManifest, parse_cleanup_manifest
from zeroth.governance.retention.errors import StaleCleanupClaimError
from zeroth.governance.retention.replay import CleanupReplayState
from zeroth.platform.storage.json import from_json_value

if TYPE_CHECKING:
    from zeroth.core.retention.audit_log_repository import RetentionAuditLogRepository
    from zeroth.core.retention.cleanup_manifest import CleanupOperation
    from zeroth.core.retention.cleanup_state_repository import (
        CleanupStateRecord,
        CleanupStateRepository,
    )
    from zeroth.core.retention.coordination import RetentionCoordinator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupClaims:
    """Reads and fenced-writes the durable claim state for one erasure authorization."""

    coordinator: RetentionCoordinator
    log: RetentionAuditLogRepository
    cleanup_state: CleanupStateRepository
    lease_seconds: float
    replay: Callable[[dict[str, Any], list[dict[str, Any]]], CleanupReplayState]

    async def load_or_materialize(
        self,
        connection: Any,
        authorization: dict[str, Any],
    ) -> CleanupReplayState:
        """Load current state in O(N), replaying audit history once only for legacy rows."""
        authorization_log_id = str(authorization["log_id"])
        materialized = await self.cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if materialized is None:
            entries = await self.log.list_for_run_in_transaction(
                connection,
                str(authorization["run_id"]),
            )
            replayed = self.replay(authorization, entries)
            await self.cleanup_state.initialize_in_transaction(
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
        operation_rows = await self.cleanup_state.list_operations_in_transaction(
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
        return CleanupReplayState(
            manifest=manifest,
            generation=materialized.generation,
            revision=materialized.revision,
            active_claim_id=materialized.active_claim_id,
            active_claim_log_id=materialized.active_claim_log_id,
            lease_expires_at=materialized.lease_expires_at,
            terminal_status=materialized.terminal_status,
            terminal_log_id=materialized.terminal_log_id,
        )

    async def state_record(
        self,
        connection: Any,
        authorization_log_id: str,
    ) -> CleanupStateRecord:
        """Return the materialized cleanup state row, replaying legacy audit history when absent."""
        state = await self.cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if state is not None:
            return state
        authorization = await self.log.get_in_transaction(connection, authorization_log_id)
        if authorization is None or authorization["action"] != "erasure_authorized":
            raise ValueError("cleanup authorization disappeared")
        await self.load_or_materialize(connection, authorization)
        state = await self.cleanup_state.get_state_in_transaction(
            connection,
            authorization_log_id,
        )
        if state is None:  # pragma: no cover - initialization is transactional
            raise RuntimeError("cleanup state initialization failed")
        return state

    @staticmethod
    def verify_active(
        state: CleanupReplayState,
        claim_id: str,
        generation: int,
    ) -> None:
        """Raise :class:`StaleCleanupClaimError` unless the claim/generation still own the state."""
        if state.active_claim_id != claim_id or state.generation != generation:
            raise StaleCleanupClaimError(
                f"cleanup claim {claim_id!r} generation {generation} is stale"
            )

    async def record_heartbeat(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        tenant_id: str,
        run_id: str,
    ) -> str:
        """Extend the active claim's lease (fenced against stale claims); return the log id."""
        async with self.coordinator.transaction(tenant_id) as transaction:
            state = await self.state_record(transaction.connection, authorization_log_id)
            self.verify_active(state, claim_id, generation)
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
            log_id = await self.log.record_in_transaction(
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
            await self.cleanup_state.heartbeat_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                lease_expires_at=lease_expires_at,
            )
            return log_id

    async def record_operation_delta(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> str:
        """Persist one operation's status delta (log entry + state row) under the claim fence."""
        async with self.coordinator.transaction(operation.tenant_id) as transaction:
            state = await self.state_record(transaction.connection, authorization_log_id)
            self.verify_active(state, claim_id, generation)
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
            log_id = await self.log.record_in_transaction(
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
            await self.cleanup_state.update_operation_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                operation=operation,
                lease_expires_at=lease_expires_at,
            )
            return log_id

    async def record_terminal(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        manifest: CleanupManifest,
        *,
        failed: bool,
    ) -> str:
        """Record the completed/failed terminal event and state, fenced against stale claims."""
        async with self.coordinator.transaction(manifest.tenant_id) as transaction:
            state = await self.state_record(transaction.connection, authorization_log_id)
            self.verify_active(state, claim_id, generation)
            terminal_status = "failed" if failed else "completed"
            log_id = await self.log.record_in_transaction(
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
            await self.cleanup_state.terminal_in_transaction(
                transaction.connection,
                authorization_log_id=authorization_log_id,
                claim_id=claim_id,
                generation=generation,
                expected_revision=state.revision,
                terminal_status=terminal_status,
                terminal_log_id=log_id,
            )
            return log_id

    async def repair_terminal(
        self,
        connection: Any,
        *,
        authorization_log_id: str,
        tenant_id: str,
        run_id: str,
        reason: str,
        generation: int,
        revision: int,
    ) -> str:
        """Write the terminal record a completed manifest never got, inside the caller's lock.

        This runs while ``retry_external_cleanup`` holds the tenant transaction it
        used to inspect the state, so it takes the connection rather than opening
        its own -- re-entering the coordinator here would deadlock on that lock.
        """
        terminal_log_id = await self.log.record_in_transaction(
            connection,
            tenant_id=tenant_id,
            run_id=run_id,
            action="external_cleanup_completed",
            reason=reason,
            detail={
                "authorization_log_id": authorization_log_id,
                "claim_id": None,
                "generation": generation,
                "revision": revision + 1,
                "repaired": True,
            },
        )
        await self.cleanup_state.repair_terminal_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            generation=generation,
            expected_revision=revision,
            terminal_log_id=terminal_log_id,
        )
        return terminal_log_id

    async def claim(
        self,
        connection: Any,
        *,
        authorization_log_id: str,
        tenant_id: str,
        run_id: str,
        reason: str,
        claim_id: str,
        generation: int,
        expected_generation: int,
        expected_revision: int,
        lease_expires_at: datetime,
    ) -> str:
        """Take the cleanup claim inside the caller's already-open tenant transaction.

        Same reason as :meth:`repair_terminal`: the caller inspected the state
        under the tenant lock and must claim without releasing it, or another
        worker slips in between the check and the claim.
        """
        claim_log_id = await self.log.record_in_transaction(
            connection,
            tenant_id=tenant_id,
            run_id=run_id,
            action="external_cleanup_claimed",
            reason=reason,
            detail={
                "authorization_log_id": authorization_log_id,
                "claim_id": claim_id,
                "generation": generation,
                "revision": expected_revision + 1,
                "lease_expires_at": lease_expires_at.isoformat(),
            },
        )
        await self.cleanup_state.claim_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
            claim_id=claim_id,
            claim_log_id=claim_log_id,
            lease_expires_at=lease_expires_at,
        )
        return claim_log_id

    async def release(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        tenant_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        """Best-effort release of an aborted claim so a later retry can re-claim immediately.

        A stale caller is a silent no-op: the claim it is trying to release has
        already been superseded, and releasing it would clear its successor's.

        Failures are logged rather than raised. The only caller is the abort path
        of ``retry_external_cleanup``, which re-raises the exception that brought
        it here -- letting this one escape would mask the original cause.
        """
        try:
            async with self.coordinator.transaction(tenant_id) as transaction:
                state = await self.state_record(transaction.connection, authorization_log_id)
                if state.active_claim_id != claim_id or state.generation != generation:
                    return
                await self.log.record_in_transaction(
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
                await self.cleanup_state.release_in_transaction(
                    transaction.connection,
                    authorization_log_id=authorization_log_id,
                    claim_id=claim_id,
                    generation=generation,
                    expected_revision=state.revision,
                )
        except Exception:
            logger.exception("failed to release external cleanup claim %s", claim_id)

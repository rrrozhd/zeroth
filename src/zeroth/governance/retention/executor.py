"""Running an authorized manifest's external cleanup under a live claim.

This is the only part of erasure that touches surfaces outside the retention
database -- the artifact store and the econ plane. Three rules hold it together:

* **Order is the manifest's order.** The prefix sweep, then each harvested key,
  then the econ deletion. Operations already ``completed`` or ``skipped`` are
  never re-run.
* **A failure is recorded, not raised.** One unreachable surface must not stop
  the operations queued behind it, or a partial erasure would stand while the
  remaining surfaces still hold the data.
* **Every operation is bracketed by two fenced deltas** -- ``in_progress``
  before, terminal status after -- so an interrupted worker's progress is
  visible to whoever retries.

The claim lease is heartbeated on a background cadence while an operation runs.
Without it a slow delete outlives its own lease and its final delta is fenced
out by the successor that took over.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zeroth.governance.retention.manifests import result_from_manifest

if TYPE_CHECKING:
    from zeroth.governance.retention.claims import CleanupClaims
    from zeroth.governance.retention.cleanup_manifest import CleanupManifest, CleanupOperation
    from zeroth.governance.retention.compatibility import CompatibilityLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupExecutor:
    """Executes the unfinished operations of one claimed cleanup manifest."""

    claims: CleanupClaims
    compatibility: CompatibilityLog
    artifact_store: object | None
    econ_eraser: object | None
    lease_seconds: float

    async def execute_claimed(
        self,
        *,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        manifest: CleanupManifest,
    ) -> str:
        """Run all unfinished manifest operations under the claim; return the terminal log id."""
        terminal_log_id = ""
        for index, operation in enumerate(manifest.operations):
            if operation.status in {"completed", "skipped"}:
                continue
            manifest.operations[index] = operation.model_copy(
                update={"status": "in_progress", "error": None}
            )
            await self.claims.record_operation_delta(
                authorization_log_id,
                claim_id,
                generation,
                manifest.operations[index],
            )
            try:
                deleted = await self.execute_operation_with_heartbeat(
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
            await self.claims.record_operation_delta(
                authorization_log_id,
                claim_id,
                generation,
                manifest.operations[index],
            )
        failed = any(operation.status == "failed" for operation in manifest.operations)
        terminal_log_id = await self.claims.record_terminal(
            authorization_log_id,
            claim_id,
            generation,
            manifest,
            failed=failed,
        )
        result = result_from_manifest(
            manifest,
            authorization_log_id=authorization_log_id,
            retry_log_id=terminal_log_id,
        )
        await self.compatibility.record_external_steps(result, manifest, failed=failed)
        return terminal_log_id

    async def execute_operation(self, operation: CleanupOperation) -> int:
        """Dispatch one operation to its external surface; return the deleted-item count."""
        if operation.kind == "artifact_prefix":
            return int(
                await self.call_with_idempotency(
                    self.artifact_store.cleanup_run,
                    operation.run_id,
                    idempotency_key=operation.operation_id,
                )
                or 0
            )
        if operation.kind == "artifact_key":
            return int(
                bool(
                    await self.call_with_idempotency(
                        self.artifact_store.delete,
                        operation.artifact_key,
                        idempotency_key=operation.operation_id,
                    )
                )
            )
        return int(
            await self.econ_eraser.delete_events_for_run(
                operation.tenant_id,
                operation.join_keys,
                idempotency_key=operation.operation_id,
            )
        )

    async def execute_operation_with_heartbeat(
        self,
        authorization_log_id: str,
        claim_id: str,
        generation: int,
        operation: CleanupOperation,
    ) -> int:
        """Run one operation while heartbeating the claim lease every third of its window."""
        task = asyncio.create_task(self.execute_operation(operation))
        interval = max(self.lease_seconds / 3, 0.01)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    return await task
                await self.claims.record_heartbeat(
                    authorization_log_id=authorization_log_id,
                    claim_id=claim_id,
                    generation=generation,
                    tenant_id=operation.tenant_id,
                    run_id=operation.run_id,
                )
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    async def call_with_idempotency(
        method: Any,
        *args: Any,
        idempotency_key: str,
    ) -> Any:
        """Await ``method`` with the operation's idempotency key forwarded."""
        return await method(*args, idempotency_key=idempotency_key)

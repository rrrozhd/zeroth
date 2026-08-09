"""Durable run worker that replaces asyncio.create_task dispatch.

A RunWorker polls SQLite for PENDING runs, claims them via lease, drives them
through the RuntimeOrchestrator, and releases the lease on completion.  On
startup it reclaims any orphaned RUNNING runs whose leases have expired.

The worker runs as a single asyncio background task started in the app lifespan.
Graceful shutdown cancels the poll loop without interrupting runs that are
currently executing (the semaphore ensures bounded concurrency).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from zeroth.contracts.governed import RunStatus
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError, LeaseManager
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotStore
from zeroth.runtime.runs import RunFailureState

if TYPE_CHECKING:
    from zeroth.contracts.graph import Graph
    from zeroth.governance.guardrails.dead_letter import DeadLetterManager
    from zeroth.platform.observability.metrics import MetricsCollector
    from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator

logger = logging.getLogger(__name__)


def _new_worker_id() -> str:
    return uuid4().hex


@dataclass
class RunWorker:
    """Long-lived worker that drives PENDING runs to completion.

    Attributes:
        deployment_ref:      The deployment this worker serves.
        run_repository:      Used to load/transition runs.
        orchestrator:        Drives execution for each run.
        graph:               The deployment graph passed to the orchestrator.
        lease_manager:       Manages SQLite-backed leases.
        max_concurrency:     Maximum simultaneous runs (default 8).
        poll_interval:       Seconds between poll ticks when idle (default 0.5).
        worker_id:           Unique ID for this worker instance.
        dead_letter_manager: Optional; marks repeatedly-failing runs as dead-letter.
        metrics_collector:   Optional; records execution metrics.
    """

    deployment_ref: str
    run_repository: RunRepository
    orchestrator: RuntimeOrchestrator
    graph: Graph
    lease_manager: LeaseManager
    tenant_id: str | None = None
    workspace_id: str | None = None
    max_concurrency: int = 8
    poll_interval: float = 0.5
    worker_id: str = field(default_factory=_new_worker_id)
    dead_letter_manager: DeadLetterManager | None = None
    metrics_collector: MetricsCollector | None = None
    shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_tasks: set[asyncio.Task] = set()
        # The in-flight drive task per run, so the renewal loop can stop the
        # work when ownership is lost rather than only logging it.
        self._active_drives: dict[str, asyncio.Task] = {}
        self._lost_leases: set[str] = set()
        self._lease_generations: dict[str, int | None] = {}
        self._stopping = False
        self._token_lifecycle = (
            TokenLifecycleAdapter(self.run_repository)
            if isinstance(self.run_repository, TokenSnapshotStore)
            else None
        )

    def _lease_scope(self) -> dict[str, object]:
        """Return the trusted deployment scope when this worker has one."""
        if self.tenant_id is None:
            return {}
        return {"tenant_id": self.tenant_id, "workspace_id": self.workspace_id}

    # ---------------------------------------------------------------------------
    # Public lifecycle
    # ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Recover orphaned runs from crashed workers, then begin the poll loop."""
        logger.info(
            "worker %s starting on %s, deployment=%s, max_concurrency=%d",
            self.worker_id,
            socket.gethostname(),
            self.deployment_ref,
            self.max_concurrency,
        )
        orphans = await self.lease_manager.claim_orphaned(
            self.deployment_ref,
            self.worker_id,
            **self._lease_scope(),
        )
        for run_id in orphans:
            logger.info("worker %s recovering orphaned run %s", self.worker_id, run_id)
            task = asyncio.create_task(
                self._execute_leased_run(run_id, is_recovery=True),
                name=f"recover-{run_id}",
            )
            self._track(task)

    async def poll_loop(self) -> None:
        """Continuously claim and dispatch PENDING runs until cancelled."""
        while not self._stopping:
            slot_reserved = False
            try:
                await self._semaphore.acquire()
                slot_reserved = True
                run_id = await self.lease_manager.claim_pending(
                    self.deployment_ref,
                    self.worker_id,
                    **self._lease_scope(),
                )
                if run_id is not None:
                    task = asyncio.create_task(
                        self._execute_leased_run(
                            run_id,
                            is_recovery=False,
                            slot_reserved=True,
                        ),
                        name=f"run-{run_id}",
                    )
                    self._track(task)
                else:
                    self._semaphore.release()
                    slot_reserved = False
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                if slot_reserved:
                    self._semaphore.release()
                raise
            except Exception:
                if slot_reserved:
                    self._semaphore.release()
                logger.exception("worker %s poll error", self.worker_id)
                await asyncio.sleep(self.poll_interval)

    # ---------------------------------------------------------------------------
    # Internal execution
    # ---------------------------------------------------------------------------

    async def _execute_leased_run(
        self,
        run_id: str,
        *,
        is_recovery: bool,
        slot_reserved: bool = False,
    ) -> None:
        """Drive one run to completion or failure under the semaphore."""
        import time

        if self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_runs_started_total")
        started_at = time.perf_counter()
        acquired_here = False
        if not slot_reserved:
            await self._semaphore.acquire()
            acquired_here = True
        # Captured right after the claim: the renewal loop presents it back so a
        # lease that was released and re-acquired is detected even when the same
        # worker id ends up holding it again.
        generation = await self.lease_manager.current_generation(run_id)
        self._lease_generations[run_id] = generation
        fence_installed = await self._install_write_fence(run_id, generation)
        drive_task = asyncio.create_task(
            self._drive_run(run_id, is_recovery=is_recovery),
            name=f"drive-{run_id}",
        )
        self._active_drives[run_id] = drive_task
        renewal_task = asyncio.create_task(
            self._renewal_loop(run_id),
            name=f"renew-{run_id}",
        )
        try:
            await drive_task
            elapsed = time.perf_counter() - started_at
            if self.metrics_collector is not None:
                self.metrics_collector.increment("zeroth_runs_completed_total")
                self.metrics_collector.observe("zeroth_run_duration_seconds", elapsed)
        except asyncio.CancelledError:
            if run_id not in self._lost_leases:
                raise
            self._record_lease_loss(run_id)
            await self._record_worker_audit(run_id, reason_code="lease_lost")
        except FencedRunWriteRejectedError:
            await self._handle_fencing_rejection(run_id)
        except Exception:
            await self._handle_run_exception(run_id)
        finally:
            if fence_installed:
                self.run_repository.clear_fence(run_id)
            renewal_task.cancel()
            # Suppress ANY outcome of the renewal task (audit B4). The normal path
            # raises CancelledError (a BaseException, so it is listed explicitly —
            # `suppress(Exception)` alone would let it through). But if
            # _renewal_loop already finished by RAISING (e.g. its lease-renewal DB
            # transaction hit "database is locked"), cancel() is a no-op on the
            # done task and `await` re-raises that non-CancelledError — which would
            # escape this finally BEFORE release_lease + semaphore.release() run,
            # permanently leaking the concurrency slot. We cancelled it and don't
            # care about its result either way.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await renewal_task
            self._active_drives.pop(run_id, None)
            self._lost_leases.discard(run_id)
            self._lease_generations.pop(run_id, None)
            # release_lease is owner-qualified, so a displaced worker calling it
            # cannot clear the new owner's lease.
            await self.lease_manager.release_lease(run_id, self.worker_id)
            if slot_reserved or acquired_here:
                self._semaphore.release()

    async def _install_write_fence(self, run_id: str, generation: int | None) -> bool:
        """ZER-26/AUD-004: fence this drive's run-state saves on the lease.

        Every save during the drive — the worker's own transitions and the
        orchestrator's, which share this repository — then carries the lease
        predicate, so a displaced worker's write is refused in the statement
        itself rather than by the (asynchronous) cancellation. Only installed
        when this worker actually holds the lease: a drive of an unclaimed run
        (tests, legacy paths) keeps its unfenced behaviour.
        """
        if generation is None or not hasattr(self.run_repository, "install_fence"):
            return False
        if await self.lease_manager.current_holder(run_id) != self.worker_id:
            return False
        self.run_repository.install_fence(run_id, self.worker_id, generation)
        return True

    async def _handle_fencing_rejection(self, run_id: str) -> None:
        """The fence fired before the renewal loop noticed: ownership moved.

        The refused write is the proof. The run is the new owner's, so no run
        state is written — only the durable evidence and the metric.
        """
        self._record_lease_loss(run_id)
        await self._record_worker_audit(run_id, reason_code="lease_fencing_rejected")
        if self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_lease_fencing_rejected_total")

    def _record_lease_loss(self, run_id: str) -> None:
        """Note that ownership moved away, and deliberately write nothing else.

        The run is not failed -- it simply is not ours any more. Marking it
        FAILED here would be a stale write on the new owner's run.
        """
        logger.warning("worker %s stopped run %s after losing its lease", self.worker_id, run_id)
        if self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_lease_lost_total")

    async def _record_worker_audit(self, run_id: str, *, reason_code: str) -> None:
        """ZER-26/AUD-008: leave a durable record of a worker-level lease event.

        Fencing rejections and lease losses previously left only a log line and
        a counter — nothing durable said *why* a worker stopped mid-run. The
        audit trail is append-only evidence, not run state, so writing it from
        a displaced worker does not violate the fence.
        """
        audit_repository = getattr(self.orchestrator, "audit_repository", None)
        if audit_repository is None:
            return
        try:
            run = await self.run_repository.get(run_id)
            if run is None:
                return
            from zeroth.governance.audit import NodeAuditRecord

            await audit_repository.write(
                NodeAuditRecord(
                    audit_id=f"{run_id}:worker:{uuid4().hex[:12]}",
                    run_id=run_id,
                    thread_id=run.thread_id,
                    tenant_id=run.tenant_id,
                    workspace_id=run.workspace_id,
                    node_id="__worker__",
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    status="rejected",
                    execution_metadata={
                        "reason_code": reason_code,
                        "worker_id": self.worker_id,
                        "lease_generation": self._lease_generations.get(run_id),
                    },
                )
            )
        except Exception:
            # Evidence writing must never mask the event it records.
            logger.exception(
                "worker %s: failed to write %s audit for run %s",
                self.worker_id,
                reason_code,
                run_id,
            )

    async def _handle_run_exception(self, run_id: str) -> None:
        """Dead-letter or fail a run whose execution raised."""
        logger.exception("worker %s run %s raised unexpectedly", self.worker_id, run_id)
        if self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_worker_crashes_total")
        # Increment failure_count and maybe dead-letter before marking failed.
        if self.dead_letter_manager is None:
            await self._mark_failed(run_id, reason="worker_exception")
            return
        dead_lettered = await self.dead_letter_manager.handle_run_failure(run_id)
        if not dead_lettered:
            await self._mark_failed(run_id, reason="worker_exception")
        elif self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_runs_dead_lettered_total")

    async def _drive_run(self, run_id: str, *, is_recovery: bool) -> None:
        """Transition the run to RUNNING and drive it through the orchestrator."""
        run = await self.run_repository.get(run_id)
        if run is None:
            logger.warning("worker %s: run %s not found, skipping", self.worker_id, run_id)
            return

        # A recovering run may already be in RUNNING state; only transition if PENDING.
        if run.status == RunStatus.PENDING:
            try:
                run = await self.run_repository.transition(run_id, RunStatus.RUNNING)
            except (ValueError, KeyError):
                logger.warning(
                    "worker %s: transition to RUNNING failed for %s", self.worker_id, run_id
                )
                return

        await self._resume_stopped_snapshot(run_id)

        # Approval-resumed runs have metadata set by schedule_continuation.
        approval_resolved_id = run.metadata.get("approval_resolved_id")
        if approval_resolved_id:
            await self._drive_approval_resumed(run, approval_resolved_id)
            return

        if is_recovery:
            recovery_cp_id = await self.lease_manager.get_recovery_checkpoint_id(run_id)
            if recovery_cp_id:
                logger.info(
                    "worker %s resuming run %s from checkpoint %s",
                    self.worker_id,
                    run_id,
                    recovery_cp_id,
                )
                await self.orchestrator.resume_graph(self.graph, run_id)
                return

        await self.orchestrator._drive(self.graph, run)

    async def _drive_approval_resumed(self, run: object, approval_id: str) -> None:
        """Resume a run that was paused for an approval and is now resolved."""
        from zeroth.contracts.graph import HumanApprovalNode
        from zeroth.governance.approvals import ApprovalService

        approval_service: ApprovalService | None = getattr(
            self.orchestrator, "approval_service", None
        )
        if approval_service is None:
            # Fall back to plain resume if approval service isn't wired.
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        record = await approval_service.get(approval_id)
        if record is None:
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        node = next(
            (
                n
                for n in self.graph.nodes
                if n.node_id == record.node_id and isinstance(n, HumanApprovalNode)
            ),
            None,
        )
        if node is None:
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        output_payload = getattr(run, "metadata", {}).get("approval_resolved_payload") or {}
        await self.orchestrator.record_approval_resolution(
            graph=self.graph,
            run=run,
            node=node,
            output_payload=output_payload,
            approval_record=record,
        )
        # Clear the approval markers so they're not replayed on a future recovery.
        run.metadata.pop("approval_resolved_id", None)
        run.metadata.pop("approval_resolved_payload", None)
        await self.run_repository.put(run)
        await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))

    async def _mark_failed(self, run_id: str, *, reason: str) -> None:
        """Best-effort: mark a run as FAILED if it is not already terminal."""
        try:
            run = await self.run_repository.get(run_id)
            if run is None:
                return
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return
            run.failure_state = RunFailureState(reason=reason, message=f"worker: {reason}")
            run.status = RunStatus.FAILED
            run.touch()
            await self.run_repository.put(run)
        except Exception:
            logger.exception("worker %s: failed to mark run %s as FAILED", self.worker_id, run_id)

    async def _renewal_loop(self, run_id: str) -> None:
        """Renew the lease every half-interval; stop the run if we lose it.

        Observing the loss is not enough. Until this cancelled the drive task,
        a displaced worker kept executing and committing alongside the worker
        that had legitimately taken the run over.
        """
        interval = max(1, self.lease_manager.lease_duration_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            generation = self._lease_generations.get(run_id)
            if not await self.lease_manager.renew_lease(
                run_id, self.worker_id, generation=generation
            ):
                logger.warning("worker %s lost lease on run %s", self.worker_id, run_id)
                self._lost_leases.add(run_id)
                drive_task = self._active_drives.get(run_id)
                if drive_task is not None and not drive_task.done():
                    drive_task.cancel()
                return

    # ---------------------------------------------------------------------------
    # Wakeup handler
    # ---------------------------------------------------------------------------

    async def handle_wakeup(self, run_id: str) -> None:
        """ARQ wakeup callback -- attempt to claim from DB immediately.

        The run_id is informational only; the worker always claims from the
        lease store (not from the ARQ job payload) per D-06.
        """
        slot_reserved = False
        try:
            await self._semaphore.acquire()
            slot_reserved = True
            claimed_id = await self.lease_manager.claim_pending(
                self.deployment_ref,
                self.worker_id,
                **self._lease_scope(),
            )
            if claimed_id is not None:
                task = asyncio.create_task(
                    self._execute_leased_run(
                        claimed_id,
                        is_recovery=False,
                        slot_reserved=True,
                    ),
                    name=f"wakeup-{claimed_id}",
                )
                self._track(task)
            else:
                self._semaphore.release()
        except Exception:
            if slot_reserved:
                self._semaphore.release()
            logger.exception("worker %s wakeup claim error", self.worker_id)

    # ---------------------------------------------------------------------------
    # Graceful shutdown
    # ---------------------------------------------------------------------------

    async def graceful_shutdown(self) -> None:
        """Wait for in-flight tasks then release remaining leases to PENDING.

        Called on SIGTERM. Steps:
        1. Set stopping flag so poll_loop exits cleanly
        2. Wait for active tasks to complete (up to shutdown_timeout)
        3. For any tasks still running, cancel them and release their leases
           back to PENDING so another worker can claim them
        """
        self._stopping = True
        if not self._active_tasks:
            return

        # Wait for active tasks to finish within timeout
        done, pending = await asyncio.wait(
            self._active_tasks,
            timeout=self.shutdown_timeout,
        )

        # Release leases for tasks that didn't finish. The drive is stopped
        # BEFORE the voluntary release: releasing first cleared the fence and
        # the lease while the drive task was still alive, reopening exactly the
        # displaced-writer window the fence exists to close (ZER-26/AUD-004).
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            run_id = self._extract_run_id(task)
            if run_id:
                await self._stop_token_snapshot(run_id)
                await self._release_to_pending(run_id)

    def _extract_run_id(self, task: asyncio.Task) -> str | None:
        """Extract run_id from a task name like 'run-abc123' or 'wakeup-abc123'."""
        name = task.get_name()
        for prefix in ("run-", "wakeup-", "recover-"):
            if name.startswith(prefix):
                return name[len(prefix) :]
        return None

    async def _release_to_pending(self, run_id: str) -> None:
        """Release lease and revert run to PENDING for another worker."""
        try:
            # A voluntary release is the one write that legitimately happens
            # after giving up the lease: the fence must come down first, or the
            # PENDING hand-back below fences *ourselves* out.
            if hasattr(self.run_repository, "clear_fence"):
                self.run_repository.clear_fence(run_id)
            await self.lease_manager.release_lease(run_id, self.worker_id)
            run = await self.run_repository.get(run_id)
            if run is not None and run.status == RunStatus.RUNNING:
                run.status = RunStatus.PENDING
                run.touch()
                await self.run_repository.put(run)
                logger.info(
                    "worker %s released run %s back to PENDING on shutdown",
                    self.worker_id,
                    run_id,
                )
        except Exception:
            logger.exception(
                "worker %s: failed to release run %s on shutdown",
                self.worker_id,
                run_id,
            )

    async def _stop_token_snapshot(self, run_id: str) -> None:
        """Best-effort durable stop; legacy runs have no token snapshot."""
        if self._token_lifecycle is None:
            return
        snapshot = await self._token_lifecycle.store.get_token_snapshot(run_id)
        if snapshot is None:
            return
        await self._token_lifecycle.stop(run_id)

    async def _resume_stopped_snapshot(self, run_id: str) -> None:
        """Turn a replayable worker stop back into ordinary schedulable work."""
        if self._token_lifecycle is None:
            return
        from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshotState

        snapshot = await self._token_lifecycle.store.get_token_snapshot(run_id)
        if snapshot is not None and snapshot.state is TokenEngineSnapshotState.STOPPED:
            await self._token_lifecycle.resume(run_id)

    # ---------------------------------------------------------------------------
    # Task tracking
    # ---------------------------------------------------------------------------

    def _track(self, task: asyncio.Task) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

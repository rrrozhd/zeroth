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
from uuid import uuid4

from zeroth.dispatch.lease import LeaseManager
from zeroth.runs import RunFailureState, RunRepository, RunStatus

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
    orchestrator: object
    graph: object
    lease_manager: LeaseManager
    max_concurrency: int = 8
    poll_interval: float = 0.5
    worker_id: str = field(default_factory=_new_worker_id)
    dead_letter_manager: object | None = None  # DeadLetterManager
    metrics_collector: object | None = None    # MetricsCollector
    shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_tasks: set[asyncio.Task] = set()
        self._stopping = False

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
        orphans = self.lease_manager.claim_orphaned(self.deployment_ref, self.worker_id)
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
                run_id = self.lease_manager.claim_pending(self.deployment_ref, self.worker_id)
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
        renewal_task = asyncio.create_task(
            self._renewal_loop(run_id),
            name=f"renew-{run_id}",
        )
        try:
            await self._drive_run(run_id, is_recovery=is_recovery)
            elapsed = time.perf_counter() - started_at
            if self.metrics_collector is not None:
                self.metrics_collector.increment("zeroth_runs_completed_total")
                self.metrics_collector.observe("zeroth_run_duration_seconds", elapsed)
        except Exception:
            logger.exception("worker %s run %s raised unexpectedly", self.worker_id, run_id)
            if self.metrics_collector is not None:
                self.metrics_collector.increment("zeroth_worker_crashes_total")
            # Increment failure_count and maybe dead-letter before marking failed.
            if self.dead_letter_manager is not None:
                dead_lettered = await asyncio.get_event_loop().run_in_executor(
                    None, self.dead_letter_manager.handle_run_failure, run_id
                )
                if not dead_lettered:
                    await self._mark_failed(run_id, reason="worker_exception")
                else:
                    if self.metrics_collector is not None:
                        self.metrics_collector.increment("zeroth_runs_dead_lettered_total")
            else:
                await self._mark_failed(run_id, reason="worker_exception")
        finally:
            renewal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal_task
            self.lease_manager.release_lease(run_id, self.worker_id)
            if slot_reserved or acquired_here:
                self._semaphore.release()

    async def _drive_run(self, run_id: str, *, is_recovery: bool) -> None:
        """Transition the run to RUNNING and drive it through the orchestrator."""
        run = self.run_repository.get(run_id)
        if run is None:
            logger.warning("worker %s: run %s not found, skipping", self.worker_id, run_id)
            return

        # A recovering run may already be in RUNNING state; only transition if PENDING.
        if run.status is RunStatus.PENDING:
            try:
                run = self.run_repository.transition(run_id, RunStatus.RUNNING)
            except (ValueError, KeyError):
                logger.warning(
                    "worker %s: transition to RUNNING failed for %s", self.worker_id, run_id
                )
                return

        # Approval-resumed runs have metadata set by schedule_continuation.
        approval_resolved_id = run.metadata.get("approval_resolved_id")
        if approval_resolved_id:
            await self._drive_approval_resumed(run, approval_resolved_id)
            return

        if is_recovery:
            recovery_cp_id = self.lease_manager.get_recovery_checkpoint_id(run_id)
            if recovery_cp_id:
                logger.info(
                    "worker %s resuming run %s from checkpoint %s",
                    self.worker_id, run_id, recovery_cp_id,
                )
                await self.orchestrator.resume_graph(self.graph, run_id)
                return

        await self.orchestrator._drive(self.graph, run)

    async def _drive_approval_resumed(self, run: object, approval_id: str) -> None:
        """Resume a run that was paused for an approval and is now resolved."""
        from zeroth.approvals import ApprovalService
        from zeroth.graph import HumanApprovalNode

        approval_service: ApprovalService | None = getattr(
            self.orchestrator, "approval_service", None
        )
        if approval_service is None:
            # Fall back to plain resume if approval service isn't wired.
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        record = approval_service.get(approval_id)
        if record is None:
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        node = next(
            (
                n for n in self.graph.nodes
                if n.node_id == record.node_id and isinstance(n, HumanApprovalNode)
            ),
            None,
        )
        if node is None:
            await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))
            return

        output_payload = getattr(run, "metadata", {}).get("approval_resolved_payload") or {}
        self.orchestrator.record_approval_resolution(
            graph=self.graph,
            run=run,
            node=node,
            output_payload=output_payload,
            approval_record=record,
        )
        # Clear the approval markers so they're not replayed on a future recovery.
        run.metadata.pop("approval_resolved_id", None)
        run.metadata.pop("approval_resolved_payload", None)
        self.run_repository.put(run)
        await self.orchestrator.resume_graph(self.graph, getattr(run, "run_id", ""))

    async def _mark_failed(self, run_id: str, *, reason: str) -> None:
        """Best-effort: mark a run as FAILED if it is not already terminal."""
        try:
            run = self.run_repository.get(run_id)
            if run is None:
                return
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return
            run.failure_state = RunFailureState(reason=reason, message=f"worker: {reason}")
            run.status = RunStatus.FAILED
            run.touch()
            self.run_repository.put(run)
        except Exception:
            logger.exception("worker %s: failed to mark run %s as FAILED", self.worker_id, run_id)

    async def _renewal_loop(self, run_id: str) -> None:
        """Background task that renews the lease every half-interval."""
        interval = max(1, self.lease_manager.lease_duration_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            if not self.lease_manager.renew_lease(run_id, self.worker_id):
                logger.warning(
                    "worker %s lost lease on run %s", self.worker_id, run_id
                )
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
            claimed_id = self.lease_manager.claim_pending(
                self.deployment_ref, self.worker_id
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

        # Release leases for tasks that didn't finish
        for task in pending:
            run_id = self._extract_run_id(task)
            if run_id:
                self._release_to_pending(run_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _extract_run_id(self, task: asyncio.Task) -> str | None:
        """Extract run_id from a task name like 'run-abc123' or 'wakeup-abc123'."""
        name = task.get_name()
        for prefix in ("run-", "wakeup-", "recover-"):
            if name.startswith(prefix):
                return name[len(prefix) :]
        return None

    def _release_to_pending(self, run_id: str) -> None:
        """Release lease and revert run to PENDING for another worker."""
        try:
            self.lease_manager.release_lease(run_id, self.worker_id)
            run = self.run_repository.get(run_id)
            if run is not None and run.status == RunStatus.RUNNING:
                run.status = RunStatus.PENDING
                run.touch()
                self.run_repository.put(run)
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

    # ---------------------------------------------------------------------------
    # Task tracking
    # ---------------------------------------------------------------------------

    def _track(self, task: asyncio.Task) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

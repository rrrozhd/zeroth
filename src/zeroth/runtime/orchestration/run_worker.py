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
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from zeroth.contracts.governed import RunStatus
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError, LeaseManager
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.runs import RunFailureState

if TYPE_CHECKING:
    from zeroth.contracts.graph import Graph
    from zeroth.governance.guardrails.dead_letter import DeadLetterManager
    from zeroth.platform.observability.metrics import MetricsCollector
    from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator

logger = logging.getLogger(__name__)


def _new_worker_id() -> str:
    return uuid4().hex


class _FenceOutcome(Enum):
    """Why this drive did or did not get a write fence.

    A single ``False`` used to carry two incompatible meanings: "there is
    nothing here to fence against" and "somebody else owns this run now". The
    caller could not tell them apart and so treated both as *proceed unfenced* —
    which, in the second case, drove a run this worker no longer held and let
    the failure handler stamp a terminal status on it with no fence to refuse
    the write.
    """

    #: We hold the lease; every save now carries the lease predicate.
    INSTALLED = "installed"
    #: Nothing to fence against (no fencing store, or an unclaimed run). Tests
    #: and legacy direct drives land here and keep their unfenced behaviour.
    UNFENCED = "unfenced"
    #: Another worker holds the lease. The run is theirs; do not drive it.
    DISPLACED = "displaced"


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
    orphan_sweep_interval: float = 5.0
    worker_id: str = field(default_factory=_new_worker_id)
    dead_letter_manager: DeadLetterManager | None = None
    metrics_collector: MetricsCollector | None = None
    shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.orphan_sweep_interval <= 0:
            raise ValueError("orphan_sweep_interval must be positive")
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_tasks: set[asyncio.Task] = set()
        # The in-flight drive task per run, so the renewal loop can stop the
        # work when ownership is lost rather than only logging it.
        self._active_drives: dict[str, asyncio.Task] = {}
        self._operator_interrupts: set[str] = set()
        self._lost_leases: set[str] = set()
        self._lease_generations: dict[str, int | None] = {}
        self._stopping = False
        self._next_orphan_sweep_at: float | None = None
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
        await self._reconcile_child_approvals()
        await self._schedule_orphan_recovery(schedule_empty=True)
        self._next_orphan_sweep_at = asyncio.get_running_loop().time() + self.orphan_sweep_interval

    async def interrupt_active_run(self, run_id: str) -> None:
        """Stop this worker's active drive without changing its persisted status.

        The admin API persists ``WAITING_INTERRUPT`` before calling this hook.
        Marking the cancellation as operator-owned lets the worker distinguish it
        from shutdown or lease loss, so the ordinary exception handler cannot
        overwrite that durable pause with ``worker_exception``.
        """
        drive = self._active_drives.get(run_id)
        if drive is None or drive.done():
            return
        self._operator_interrupts.add(run_id)
        drive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drive

    async def _schedule_orphan_recovery(self, *, schedule_empty: bool = False) -> None:
        """Claim every currently expired lease and schedule bounded recovery."""
        orphans = await self.lease_manager.claim_orphaned(
            self.deployment_ref, self.worker_id, **self._lease_scope()
        )
        # Recovery obeys the same concurrency bound as the poll loop. Creating a
        # task per orphan meant a crash with a large backlog dispatched the whole
        # backlog at once, ignoring max_concurrency entirely.
        if len(orphans) > self.max_concurrency:
            logger.warning(
                "worker %s recovering %d orphaned runs at concurrency %d; "
                "the remainder start as slots free",
                self.worker_id,
                len(orphans),
                self.max_concurrency,
            )
        # Named outside the run-/wakeup-/recover- namespace on purpose: this is
        # the recovery *loop*, not a run. _extract_run_id parses any of those
        # prefixes as a run id, so a "recover-orphans-w1" task made
        # graceful_shutdown drive clear_fence and release_lease against a
        # fabricated run called "orphans-w1".
        if orphans or schedule_empty:
            task = asyncio.create_task(
                self._recover_orphans(orphans),
                name=f"orphan-recovery-loop-{self.worker_id}-{uuid4().hex[:8]}",
            )
            self._track(task)

    async def _sweep_orphans_if_due(self) -> None:
        """Revisit RUNNING leases that can expire after this process starts."""
        now = asyncio.get_running_loop().time()
        if self._next_orphan_sweep_at is not None and now < self._next_orphan_sweep_at:
            return
        self._next_orphan_sweep_at = now + self.orphan_sweep_interval
        await self._reconcile_child_approvals()
        await self._schedule_orphan_recovery()

    async def _reconcile_child_approvals(self) -> None:
        """Requeue child approvals resolved before their parent notification.

        This runs at startup and on the existing orphan-sweep cadence.  The
        approval service owns authorization, signing and compare-and-set
        semantics; the worker supplies only its deployment identity.
        """
        approval_service = getattr(self.orchestrator, "approval_service", None)
        reconcile = getattr(approval_service, "reconcile_ancestor_continuations", None)
        if reconcile is None:
            return
        await reconcile(
            deployment_ref=self.deployment_ref,
            graph_version_ref=f"{self.graph.graph_id}:v{self.graph.version}",
        )

    async def _recover_orphans(self, orphans: Sequence[str]) -> None:
        """Re-execute *orphans*, never more than ``max_concurrency`` in flight.

        The gate is a local count of live recovery tasks, deliberately not the
        worker's slot semaphore. ``_execute_leased_run`` acquires and releases
        that semaphore itself, and it does so around a window with unguarded
        awaits: acquiring here as well would either double-release or, if one of
        those awaits raised, drain the semaphore and wedge this loop forever.
        Slot ownership stays entirely with the callee.
        """
        in_flight: set[asyncio.Task[None]] = set()
        for index, run_id in enumerate(orphans):
            if self._stopping:
                # These were CLAIMED by this worker, so they are leased to a
                # process that is leaving. They have no task, so
                # graceful_shutdown's "for task in pending" cannot see them, and
                # without this they would sit RUNNING until the lease TTL
                # expired. Hand them back explicitly instead.
                undispatched = list(orphans[index:])
                logger.info(
                    "worker %s stopping; releasing %d claimed but undispatched orphaned runs",
                    self.worker_id,
                    len(undispatched),
                )
                for pending_run_id in undispatched:
                    await self._release_to_pending(pending_run_id)
                break
            while len(in_flight) >= self.max_concurrency:
                _done, in_flight = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
            logger.info("worker %s recovering orphaned run %s", self.worker_id, run_id)
            task = asyncio.create_task(
                self._execute_leased_run(run_id, is_recovery=True),
                name=f"recover-{run_id}",
            )
            in_flight.add(task)
            self._track(task)

    async def poll_loop(self) -> None:
        """Continuously claim and dispatch PENDING runs until cancelled."""
        while not self._stopping:
            slot_reserved = False
            try:
                await self._sweep_orphans_if_due()
                await self._semaphore.acquire()
                slot_reserved = True
                if self._stopping:
                    # The flag can be set while this call is parked on a busy
                    # semaphore, and the permit that frees it typically comes
                    # from a drive the shutdown just cancelled — whose run is
                    # handed back to PENDING and is therefore claimable again.
                    # Claiming it here would pull it straight back out and
                    # dispatch it into a worker that is leaving, after
                    # graceful_shutdown's wait has already passed, so nothing
                    # would await the new task.
                    self._semaphore.release()
                    return
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
        # A06-2: everything the finally has to undo is declared BEFORE the
        # permit is acquired, and the try opens immediately after it. The setup
        # below (generation read, fence install, two task spawns) used to sit
        # outside the try: a throw there skipped the whole finally, so the
        # permit, the lease, the fence and the bookkeeping all leaked for the
        # worker's lifetime — and a drive task spawned just before the throw
        # kept executing with no renewal loop watching its lease.
        fence_installed = False
        drive_task: asyncio.Task | None = None
        renewal_task: asyncio.Task | None = None
        # F-02: stays False until this run's disposition has actually been
        # decided — the drive's outcome was processed, or the run was
        # deliberately abandoned to the worker that now owns it. A setup throw
        # leaves it False, and so does the one drive outcome that is explicitly
        # *undecided* (F-10c: a spent token-snapshot CAS budget, which
        # _await_drive_outcome reports by returning False). The cleanup then
        # hands the run BACK TO PENDING rather than merely dropping the lease.
        # Dropping the lease alone is right on the poll path (claim_pending
        # leaves the run PENDING, so the next poll re-claims it) and strands
        # the run on the RECOVERY path: claim_orphaned needs status=RUNNING
        # with a non-NULL lease and release_lease NULLs it, claim_pending needs
        # status=PENDING — the run then matched no claim path and no sweeper at
        # all.
        disposition_settled = False
        if not slot_reserved:
            await self._semaphore.acquire()
            acquired_here = True
        try:
            # Captured right after the claim: the renewal loop presents it back so a
            # lease that was released and re-acquired is detected even when the same
            # worker id ends up holding it again.
            generation = await self.lease_manager.current_generation(run_id)
            self._lease_generations[run_id] = generation
            fence = await self._install_write_fence(run_id, generation)
            if fence is _FenceOutcome.DISPLACED:
                await self._abandon_displaced_run(run_id)
                disposition_settled = True
                return
            fence_installed = fence is _FenceOutcome.INSTALLED
            drive_task = asyncio.create_task(
                self._drive_run(run_id, is_recovery=is_recovery),
                name=f"drive-{run_id}",
            )
            self._active_drives[run_id] = drive_task
            renewal_task = asyncio.create_task(
                self._renewal_loop(run_id),
                name=f"renew-{run_id}",
            )
            # The drive's OWN outcome is handled by _await_drive_outcome and
            # nowhere else. A setup failure above is not a run failure:
            # converting it would mark a run FAILED over a transient lease-store
            # read, and — when the fence install itself is what threw — issue
            # that terminal write unfenced, which is exactly what ZER-26/AUD-004
            # forbids. Setup throws propagate untouched; only the cleanup below
            # is now guaranteed.
            disposition_settled = await self._await_drive_outcome(run_id, drive_task, started_at)
        finally:
            try:
                await self._cleanup_after_drive(
                    run_id,
                    drive_task=drive_task,
                    renewal_task=renewal_task,
                    fence_installed=fence_installed,
                    hand_back=not disposition_settled,
                )
            finally:
                # A06-2, the half that was still open: the permit release sits
                # where NO preceding cleanup step can skip it. It used to follow
                # an unguarded ``release_lease`` — a transaction and an UPDATE —
                # so "database is locked" there leaked the slot permanently,
                # which is the very outcome the renewal-task suppression a few
                # lines up exists to prevent.
                if slot_reserved or acquired_here:
                    self._semaphore.release()

    async def _cleanup_after_drive(
        self,
        run_id: str,
        *,
        drive_task: asyncio.Task | None,
        renewal_task: asyncio.Task | None,
        fence_installed: bool,
        hand_back: bool,
    ) -> None:
        """Undo everything the setup installed and give the run back.

        Extracted from ``_execute_leased_run``'s ``finally`` so the permit
        release can sit in an enclosing ``finally`` of its own, and to keep the
        caller under the mccabe ceiling the commit gate ratchets.
        """
        # Stop the drive BEFORE the fence comes down and the lease goes
        # back — the same ordering graceful_shutdown relies on. On the
        # normal path the task is already done and this is a no-op; it only
        # bites when the setup threw after the spawn.
        if drive_task is not None and not drive_task.done():
            drive_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await drive_task
        if fence_installed:
            self.run_repository.clear_fence(run_id)
        # Suppress ANY outcome of the renewal task (audit B4). The normal path
        # raises CancelledError (a BaseException, so it is listed explicitly —
        # `suppress(Exception)` alone would let it through). But if
        # _renewal_loop already finished by RAISING (e.g. its lease-renewal DB
        # transaction hit "database is locked"), cancel() is a no-op on the
        # done task and `await` re-raises that non-CancelledError — which would
        # escape this cleanup BEFORE the lease goes back. We cancelled it and
        # don't care about its result either way. It is None when the setup
        # threw before the spawn.
        if renewal_task is not None:
            renewal_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await renewal_task
        self._active_drives.pop(run_id, None)
        self._operator_interrupts.discard(run_id)
        self._lost_leases.discard(run_id)
        self._lease_generations.pop(run_id, None)
        # F-02: an abnormal exit leaves the run with no decided disposition, so
        # it is handed back to PENDING — the same treatment _recover_orphans
        # gives an orphan it claimed but never dispatched. Qualified on still
        # owning the lease: if the setup threw *after* we were displaced, that
        # hand-back would be an unfenced write flipping the new owner's RUNNING
        # run to PENDING. A throw from current_holder itself leaves the lease
        # untouched, so the run stays recoverable by expiry rather than lost.
        if hand_back and await self.lease_manager.current_holder(run_id) == self.worker_id:
            await self._release_to_pending(run_id)
            return
        # release_lease is owner-qualified, so a displaced worker calling it
        # cannot clear the new owner's lease.
        await self.lease_manager.release_lease(run_id, self.worker_id)

    async def _await_drive_outcome(
        self, run_id: str, drive_task: asyncio.Task, started_at: float
    ) -> bool:
        """Await the drive and convert ONLY the drive's own outcome.

        Sits outside ``_execute_leased_run`` to keep that function under the
        mccabe ceiling the commit gate ratchets, and it draws exactly the
        boundary the inline ``try`` drew: it is entered only once the fence is
        installed and the drive is spawned, so a *setup* throw still propagates
        untouched rather than routing into ``_handle_run_exception``. That
        distinction is load-bearing — routing setup failures here would mark a
        run FAILED over a transient lease-store read and, when the fence
        install itself threw, issue that terminal write unfenced
        (ZER-26/AUD-004). Cleanup stays with the caller's ``finally``, and the
        bare ``raise`` below still re-raises the cancellation into it.

        Returns:
            Whether this run's disposition is now decided. Every branch decides
            it — completion, a lost lease, a fencing rejection, a failure — and
            returns ``True``. Only the F-10c contention branch returns
            ``False``, which is what routes the run through the caller's
            existing hand-back to PENDING (ownership-qualified, fence already
            down) instead of leaving it where the drive stopped.
        """
        import time

        try:
            await drive_task
            elapsed = time.perf_counter() - started_at
            if self.metrics_collector is not None:
                self.metrics_collector.increment("zeroth_runs_completed_total")
                self.metrics_collector.observe("zeroth_run_duration_seconds", elapsed)
        except asyncio.CancelledError:
            if run_id in self._operator_interrupts:
                return True
            if run_id not in self._lost_leases:
                raise
            self._record_lease_loss(run_id)
            await self._record_worker_audit(run_id, reason_code="lease_lost")
        except FencedRunWriteRejectedError:
            await self._handle_fencing_rejection(run_id)
        except TokenSnapshotConcurrencyError:
            await self._handle_snapshot_contention(run_id)
            return False
        except Exception:
            await self._handle_run_exception(run_id)
        return True

    async def _handle_snapshot_contention(self, run_id: str) -> None:
        """F-10c: a spent CAS budget is a retry signal, not a verdict on the run.

        The bounded retry the token runtime grew is correct — the unbounded
        spin it replaced was worse — but it created an exception boundary
        nothing handled. ``_claim``/``_recover``/``_transition`` now raise
        ``TokenSnapshotConcurrencyError`` out of the drive, and it landed in
        ``except Exception`` → ``_handle_run_exception`` → ``_mark_failed``: a
        run declared permanently FAILED after eight full-jitter attempts under
        a 250 ms ceiling, i.e. **well under one second** of contention. That is
        a strictly worse availability posture than the spin.

        So the exhaustion is classified as *retryable at the worker boundary*.
        The caller hands the run back to PENDING and a later claim re-drives it
        from its persisted snapshot. Raising the budget instead was the other
        option on the table and it only moves the cliff: sustained contention
        still ends in the same terminal write, only later and while holding a
        concurrency slot the whole time. It is also not reachable from here —
        the drive-path budget lives in the token runtime/scheduler, not in the
        adapter this worker owns.

        The accepted cost is churn: under *permanent* contention the run cycles
        PENDING → RUNNING → PENDING instead of settling. That is deliberate.
        Routing it through ``dead_letter_manager`` or bumping ``failure_count``
        would count platform contention as run failure and re-derive the same
        terminal verdict a few cycles later. Only the metric and the append-only
        audit record grow, which is what makes the churn visible to operators.
        """
        logger.warning(
            "worker %s: run %s exhausted its token-snapshot CAS budget; "
            "handing it back to PENDING for a later claim",
            self.worker_id,
            run_id,
        )
        if self.metrics_collector is not None:
            self.metrics_collector.increment("zeroth_run_snapshot_contention_total")
        await self._record_worker_audit(run_id, reason_code="token_snapshot_contention")

    async def _install_write_fence(self, run_id: str, generation: int | None) -> _FenceOutcome:
        """ZER-26/AUD-004: fence this drive's run-state saves on the lease.

        Every save during the drive — the worker's own transitions and the
        orchestrator's, which share this repository — then carries the lease
        predicate, so a displaced worker's write is refused in the statement
        itself rather than by the (asynchronous) cancellation.

        The caller must distinguish the two reasons no fence gets installed, so
        this reports which one applies:

        * no fencing store, or **no lease holder at all** — an unclaimed run,
          which is how tests and legacy direct drives arrive here. There is
          nothing to be displaced by, and a fence keyed on a lease nobody holds
          would refuse every save, so the drive stays unfenced (``UNFENCED``).
        * **another worker holds the lease** — we have already been displaced
          (``DISPLACED``). Driving on would run the whole execution unfenced
          against somebody else's run.
        """
        if generation is None or not hasattr(self.run_repository, "install_fence"):
            return _FenceOutcome.UNFENCED
        holder = await self.lease_manager.current_holder(run_id)
        if holder is None:
            return _FenceOutcome.UNFENCED
        if holder != self.worker_id:
            return _FenceOutcome.DISPLACED
        self.run_repository.install_fence(run_id, self.worker_id, generation)
        return _FenceOutcome.INSTALLED

    async def _abandon_displaced_run(self, run_id: str) -> None:
        """F-07: ownership moved before the drive started — leave the run alone.

        This is the same event as a fencing rejection, observed one step
        earlier: the run belongs to another worker. Driving it would be an
        unfenced execution on somebody else's run, ending in a ``_mark_failed``
        that writes a terminal status the fence exists to refuse. As with
        ``_handle_fencing_rejection``, the only things written are the
        append-only evidence and the metric — never run state.
        """
        self._record_lease_loss(run_id)
        await self._record_worker_audit(run_id, reason_code="lease_lost_before_start")

    async def _handle_fencing_rejection(self, run_id: str) -> None:
        """Handle a fence that fired before the renewal loop noticed ownership moved.

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
                    campaign_id=(
                        str(run.metadata["campaign_id"])
                        if run.metadata.get("campaign_id") is not None
                        else None
                    ),
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
        """Best-effort durable stop; legacy runs have no token snapshot.

        Guarded exactly as ``_release_to_pending`` above guards itself, and for
        the same reason: both run inside ``graceful_shutdown``'s release loop,
        one right after the other. ``stop`` re-raises
        ``TokenSnapshotConcurrencyError`` once its bounded CAS retry budget is
        exhausted, and unguarded that skipped the ``_release_to_pending`` for
        this run *and every run after it in the batch* — one contended snapshot
        left the remainder RUNNING against a worker that was leaving.
        """
        if self._token_lifecycle is None:
            return
        try:
            snapshot = await self._token_lifecycle.store.get_token_snapshot(run_id)
            if snapshot is None:
                return
            await self._token_lifecycle.stop(run_id)
        except Exception:
            logger.exception(
                "worker %s: failed to stop the token snapshot for run %s on shutdown",
                self.worker_id,
                run_id,
            )

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
        task.add_done_callback(self._report_task_outcome)

    @staticmethod
    def _report_task_outcome(task: asyncio.Task) -> None:
        """Retrieve a finished task's exception so it is logged, not swallowed.

        ``discard`` was the only done-callback, so nothing ever called
        ``task.exception()``. A run that raised outside its own error handling
        surfaced nowhere but a garbage-collection warning, if at all.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "worker task %s failed: %s: %s",
                task.get_name(),
                type(exc).__name__,
                exc,
                exc_info=exc,
            )

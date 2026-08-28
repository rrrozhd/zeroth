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
import hashlib
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from zeroth.contracts.governed import RunStatus
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.dispatch.lease import (
    FencedRunWriteRejectedError,
    LeaseClaimResult,
    LeaseManager,
)
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.runs import RunFailureState

if TYPE_CHECKING:
    from zeroth.contracts.graph import Graph
    from zeroth.governance.guardrails.dead_letter import DeadLetterManager
    from zeroth.governance.guardrails.policy import GuardrailPolicyRepository
    from zeroth.platform.observability.metrics import MetricsCollector
    from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator

logger = logging.getLogger(__name__)


def _new_worker_id() -> str:
    return uuid4().hex


def _typed_audit_identity(value: str | int | None) -> list[str | int]:
    """Encode null and values as distinct canonical identity components."""
    return ["null"] if value is None else ["value", value]


def _concurrency_audit_digest(
    tenant_id: str,
    workspace_id: str | None,
    deployment_ref: str,
    max_concurrency: int | None,
) -> str:
    canonical = json.dumps(
        [
            "zeroth.guardrail.concurrency-audit.v1",
            _typed_audit_identity(tenant_id),
            _typed_audit_identity(workspace_id),
            _typed_audit_identity(deployment_ref),
            _typed_audit_identity(max_concurrency),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:24]


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
    guardrail_policy_repository: GuardrailPolicyRepository | None = field(
        default=None, init=False, repr=False
    )
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
        self._lost_leases: set[tuple[str, int | None]] = set()
        self._lease_generations: dict[str, int | None] = {}
        self._stopping = False
        self._next_orphan_sweep_at: float | None = None
        self._orphan_recovery_task: asyncio.Task | None = None
        self._token_lifecycle = (
            TokenLifecycleAdapter(self.run_repository)
            if isinstance(self.run_repository, TokenSnapshotStore)
            else None
        )

    def _lease_scope(self) -> dict[str, object]:
        """Return the exact deployment scope, including native default compatibility."""
        return {
            "tenant_id": self.tenant_id or "default",
            "workspace_id": self.workspace_id,
        }

    def _uses_shared_concurrency(self) -> bool:
        return self.guardrail_policy_repository is not None

    async def _effective_max_concurrency(self) -> int:
        """Resolve the shared live limit with the static worker ceiling fallback."""
        repository = self.guardrail_policy_repository
        if repository is None:
            return self.max_concurrency
        return (await repository.effective(self.deployment_ref)).max_concurrency

    async def _claim_pending(self) -> str | None:
        """Claim once and attribute saturation from that same atomic result."""
        limit = await self._effective_max_concurrency()
        result = await self.lease_manager.claim_pending_result(
            self.deployment_ref,
            self.worker_id,
            max_concurrency=limit if self._uses_shared_concurrency() else None,
            **self._lease_scope(),
        )
        if result.concurrency_saturated:
            await self._record_concurrency_saturation(result)
        return result.run_id

    async def _record_concurrency_saturation(self, result: LeaseClaimResult) -> None:
        """Record process telemetry and bounded durable evidence for saturation."""
        if self.metrics_collector is not None:
            self.metrics_collector.increment(
                "zeroth_guardrail_rejections_total", labels={"reason": "concurrency"}
            )
            self.metrics_collector.gauge_set(
                "zeroth_guardrail_utilization_ratio",
                result.utilization,
                labels={"resource": "concurrency"},
            )
        audit_repository = getattr(self.orchestrator, "audit_repository", None)
        if audit_repository is None:
            return
        await self._write_concurrency_audit(audit_repository, result)

    async def _write_concurrency_audit(
        self,
        audit_repository: AuditRepository,
        result: LeaseClaimResult,
    ) -> None:
        """Persist one deduplicated saturation record per scoped effective limit."""
        from zeroth.governance.audit import NodeAuditRecord
        from zeroth.governance.audit.errors import DuplicateAuditIdError

        tenant_id = self.tenant_id or "default"
        digest = _concurrency_audit_digest(
            tenant_id,
            self.workspace_id,
            self.deployment_ref,
            result.max_concurrency,
        )
        try:
            await audit_repository.write(
                NodeAuditRecord(
                    audit_id=f"guardrail-concurrency:{digest}",
                    run_id=f"guardrail-concurrency:{digest}",
                    tenant_id=tenant_id,
                    workspace_id=self.workspace_id,
                    node_id="service.guardrail.concurrency",
                    graph_version_ref=f"guardrail:{self.deployment_ref}",
                    deployment_ref=self.deployment_ref,
                    status="rejected",
                    execution_metadata={
                        "active_count": result.active_count,
                        "effective_limit": result.max_concurrency,
                        "reason_code": "concurrency",
                        "utilization": result.utilization,
                    },
                )
            )
        except DuplicateAuditIdError:
            return
        except Exception:
            logger.exception("worker %s failed to persist concurrency saturation", self.worker_id)

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
        await self._schedule_orphan_recovery()
        self._next_orphan_sweep_at = (
            asyncio.get_running_loop().time() + self.orphan_sweep_interval
        )

    async def interrupt_active_run(self, run_id: str) -> None:
        """Stop this worker's active drive without changing persisted status."""
        drive = self._active_drives.get(run_id)
        if drive is None or drive.done():
            return
        self._operator_interrupts.add(run_id)
        drive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drive

    async def _schedule_orphan_recovery(self) -> None:
        """Start one bounded recovery loop when no prior sweep is active."""
        if self._orphan_recovery_task is not None and not self._orphan_recovery_task.done():
            return
        # Named outside the run-/wakeup-/recover- namespace on purpose: this is
        # the recovery *loop*, not a run. _extract_run_id parses any of those
        # prefixes as a run id, so a "recover-orphans-w1" task made
        # graceful_shutdown drive clear_fence and release_lease against a
        # fabricated run called "orphans-w1".
        task = asyncio.create_task(
            self._recover_orphans(),
            name=f"orphan-recovery-loop-{self.worker_id}-{uuid4().hex[:8]}",
        )
        self._orphan_recovery_task = task
        self._track(task)

    async def _sweep_orphans_if_due(self) -> None:
        """Revisit leases and child approvals that can settle after startup."""
        now = asyncio.get_running_loop().time()
        if self._next_orphan_sweep_at is not None and now < self._next_orphan_sweep_at:
            return
        self._next_orphan_sweep_at = now + self.orphan_sweep_interval
        await self._reconcile_child_approvals()
        await self._schedule_orphan_recovery()

    async def _reconcile_child_approvals(self) -> None:
        """Requeue child approvals resolved before their parent notification."""
        approval_service = getattr(self.orchestrator, "approval_service", None)
        reconcile = getattr(approval_service, "reconcile_ancestor_continuations", None)
        if reconcile is None:
            return
        await reconcile(
            deployment_ref=self.deployment_ref,
            graph_version_ref=f"{self.graph.graph_id}:v{self.graph.version}",
        )

    async def _recover_orphans(self) -> None:
        """Reserve one local permit before each bounded orphan claim."""
        while not self._stopping:
            slot_reserved = False
            try:
                await self._semaphore.acquire()
                slot_reserved = True
                if self._stopping:
                    return
                limit = await self._effective_max_concurrency()
                result = await self.lease_manager.claim_orphaned_result(
                    self.deployment_ref,
                    self.worker_id,
                    max_concurrency=limit if self._uses_shared_concurrency() else None,
                    claim_limit=1,
                    **self._lease_scope(),
                )
                if not result.run_ids and not result.concurrency_saturated:
                    return
                if result.concurrency_saturated:
                    self._semaphore.release()
                    slot_reserved = False
                    await asyncio.sleep(max(0.01, self.poll_interval))
                    continue
                run_id = result.run_ids[0]
                # Remember the generation the claim installed. The setup window
                # re-reads it, and if that read throws the worker would otherwise
                # hold a lease it cannot name -- leaving the run claimable by
                # neither predicate until natural expiry. Recorded here because
                # the claim is the one moment the value is known for certain.
                claimed_generation = result.generations.get(run_id)
                if claimed_generation is not None:
                    self._lease_generations[run_id] = claimed_generation
                logger.info("worker %s recovering orphaned run %s", self.worker_id, run_id)
                task = asyncio.create_task(
                    self._execute_leased_run(
                        run_id,
                        is_recovery=True,
                        slot_reserved=True,
                    ),
                    name=f"recover-{run_id}",
                )
                self._track(task)
                slot_reserved = False
            finally:
                if slot_reserved:
                    self._semaphore.release()

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
                run_id = await self._claim_pending()
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
                    if slot_reserved:
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
        generation: int | None = None
        generation_registered = False
        setup_complete = False
        shutdown_cancelled = False
        disposition_settled = True
        if not slot_reserved:
            await self._semaphore.acquire()
            acquired_here = True
        try:
            # Captured right after the claim: the renewal loop presents it back so a
            # lease that was released and re-acquired is detected even when the same
            # worker id ends up holding it again.
            generation = await self.lease_manager.current_generation(run_id, **self._lease_scope())
            active_drive = self._active_drives.get(run_id)
            active_generation = self._lease_generations.get(run_id)
            if active_drive is not None and not active_drive.done():
                logger.info(
                    "worker %s refused duplicate local execution for run %s "
                    "(active generation %s, claimed generation %s)",
                    self.worker_id,
                    run_id,
                    active_generation,
                    generation,
                )
                return
            self._lease_generations[run_id] = generation
            generation_registered = True
            if not await self._lease_allows_execution(run_id, generation):
                logger.info(
                    "worker %s refused stale lease execution for run %s",
                    self.worker_id,
                    run_id,
                )
                return
            fence_installed = await self._install_write_fence(run_id, generation)
            if generation not in (None, 0) and not fence_installed:
                logger.info(
                    "worker %s refused unfenced lease execution for run %s",
                    self.worker_id,
                    run_id,
                )
                return
            if self.metrics_collector is not None:
                self.metrics_collector.increment("zeroth_runs_started_total")
            drive_task = asyncio.create_task(
                self._drive_run(run_id, is_recovery=is_recovery),
                name=f"drive-{run_id}",
            )
            self._active_drives[run_id] = drive_task
            renewal_task = asyncio.create_task(
                self._renewal_loop(run_id, generation, drive_task),
                name=f"renew-{run_id}",
            )
            setup_complete = True
            # The drive's OWN outcome is handled by _await_drive_outcome and
            # nowhere else. A setup failure above is not a run failure:
            # converting it would mark a run FAILED over a transient lease-store
            # read, and — when the fence install itself is what threw — issue
            # that terminal write unfenced, which is exactly what ZER-26/AUD-004
            # forbids. Setup throws propagate untouched; only the cleanup below
            # is now guaranteed.
            disposition_settled = await self._await_drive_outcome(
                run_id, generation, drive_task, started_at
            )
        except asyncio.CancelledError:
            shutdown_cancelled = self._stopping
            raise
        finally:
            try:
                # Stop the drive BEFORE the fence comes down and the lease goes
                # back — the same ordering graceful_shutdown relies on.
                if drive_task is not None and not drive_task.done():
                    drive_task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await drive_task
                if fence_installed and generation is not None:
                    self.run_repository.clear_fence(run_id, self.worker_id, generation)
                if renewal_task is not None:
                    renewal_task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await renewal_task
                if self._active_drives.get(run_id) is drive_task:
                    self._active_drives.pop(run_id, None)
                self._operator_interrupts.discard(run_id)
                lease_identity = (run_id, generation)
                if generation_registered and self._lease_generations.get(run_id) == generation:
                    self._lost_leases.discard(lease_identity)
                    self._lease_generations.pop(run_id, None)
                if not shutdown_cancelled:
                    if is_recovery and not setup_complete:
                        await self._hand_back_failed_recovery_setup(run_id, generation)
                    elif not disposition_settled:
                        await self._release_to_pending(run_id, generation=generation)
                    elif generation is not None:
                        # Exact generation ownership prevents stale cleanup from
                        # clearing a same-worker replay's newer lease.
                        await self.lease_manager.release_lease(
                            run_id,
                            self.worker_id,
                            generation=generation,
                            **self._lease_scope(),
                        )
            finally:
                if slot_reserved or acquired_here:
                    self._semaphore.release()

    async def _hand_back_failed_recovery_setup(
        self,
        run_id: str,
        generation: int | None,
    ) -> None:
        """Expire owned recovery work, or retain natural expiry without a generation."""
        if generation is None:
            # The setup read failed before it could name the lease. Fall back to
            # the generation recorded when this worker claimed the orphan, which
            # keeps the hand-back FENCED: an exact generation still guards the
            # write, so a lease another worker has since reclaimed is untouched.
            generation = self._lease_generations.get(run_id)
        if generation is None:
            return
        try:
            await self.lease_manager.expire_recovery_lease(
                run_id,
                self.worker_id,
                generation=generation,
                **self._lease_scope(),
            )
        except Exception:
            logger.exception(
                "worker %s retained recovery lease after setup failure for run %s",
                self.worker_id,
                run_id,
            )

    async def _lease_allows_execution(self, run_id: str, generation: int | None) -> bool:
        """Refuse expired or reclaimed leases before user code can start."""
        if generation is None:
            return False
        holder = await self.lease_manager.current_holder(run_id, **self._lease_scope())
        if holder is None:
            return generation == 0
        if holder != self.worker_id:
            return False
        return await self.lease_manager.renew_lease(
            run_id,
            self.worker_id,
            generation=generation,
            **self._lease_scope(),
        )

    async def _await_drive_outcome(
        self,
        run_id: str,
        generation: int | None,
        drive_task: asyncio.Task,
        started_at: float,
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
            if (run_id, generation) not in self._lost_leases:
                raise
            self._record_lease_loss(run_id)
            await self._record_worker_audit(
                run_id,
                reason_code="lease_lost",
                generation=generation,
            )
        except FencedRunWriteRejectedError:
            await self._handle_fencing_rejection(run_id, generation)
        except TokenSnapshotConcurrencyError:
            await self._handle_snapshot_contention(run_id, generation)
            return False
        except Exception:
            await self._handle_run_exception(run_id)
        return True

    async def _handle_snapshot_contention(
        self, run_id: str, generation: int | None
    ) -> None:
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
        await self._record_worker_audit(
            run_id,
            reason_code="token_snapshot_contention",
            generation=generation,
        )

    async def _install_write_fence(self, run_id: str, generation: int | None) -> bool:
        """ZER-26/AUD-004: fence this drive's run-state saves on the lease.

        Every save during the drive — the worker's own transitions and the
        orchestrator's, which share this repository — then carries the lease
        predicate, so a displaced worker's write is refused in the statement
        itself rather than by the (asynchronous) cancellation. Only installed
        when this worker actually holds the lease.
        """
        if generation is None or not hasattr(self.run_repository, "install_fence"):
            return False
        if await self.lease_manager.current_holder(run_id, **self._lease_scope()) != self.worker_id:
            return False
        self.run_repository.install_fence(run_id, self.worker_id, generation)
        return True

    async def _handle_fencing_rejection(
        self,
        run_id: str,
        generation: int | None,
    ) -> None:
        """Handle a fence that fired before the renewal loop noticed ownership moved.

        The refused write is the proof. The run is the new owner's, so no run
        state is written — only the durable evidence and the metric.
        """
        self._record_lease_loss(run_id)
        await self._record_worker_audit(
            run_id,
            reason_code="lease_fencing_rejected",
            generation=generation,
        )
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

    async def _record_worker_audit(
        self,
        run_id: str,
        *,
        reason_code: str,
        generation: int | None,
    ) -> None:
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
                        "lease_generation": generation,
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
            recovery_cp_id = await self.lease_manager.get_recovery_checkpoint_id(
                run_id, **self._lease_scope()
            )
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

    async def _renewal_loop(
        self,
        run_id: str,
        generation: int | None,
        drive_task: asyncio.Task,
    ) -> None:
        """Renew the lease every half-interval; stop the run if we lose it.

        Observing the loss is not enough. Until this cancelled the drive task,
        a displaced worker kept executing and committing alongside the worker
        that had legitimately taken the run over.
        """
        interval = max(1, self.lease_manager.lease_duration_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            if not await self.lease_manager.renew_lease(
                run_id,
                self.worker_id,
                generation=generation,
                **self._lease_scope(),
            ):
                logger.warning("worker %s lost lease on run %s", self.worker_id, run_id)
                self._lost_leases.add((run_id, generation))
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
            claimed_id = await self._claim_pending()
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
                if slot_reserved:
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
            run_id = self._extract_run_id(task)
            generation = None if run_id is None else self._lease_generations.get(run_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if run_id:
                await self._stop_token_snapshot(run_id)
                await self._release_to_pending(run_id, generation=generation)

    def _extract_run_id(self, task: asyncio.Task) -> str | None:
        """Extract run_id from a task name like 'run-abc123' or 'wakeup-abc123'."""
        name = task.get_name()
        for prefix in ("run-", "wakeup-", "recover-"):
            if name.startswith(prefix):
                return name[len(prefix) :]
        return None

    async def _release_to_pending(
        self,
        run_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        """Atomically hand this exact lease generation back to PENDING."""
        try:
            if generation is None:
                generation = self._lease_generations.get(run_id)
            if generation is None:
                return
            handed_back = await self.lease_manager.hand_back_to_pending(
                run_id,
                self.worker_id,
                generation=generation,
                **self._lease_scope(),
            )
            if hasattr(self.run_repository, "clear_fence"):
                self.run_repository.clear_fence(run_id, self.worker_id, generation)
            if handed_back:
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

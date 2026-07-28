"""The delivery stage's consume-write-retry loop.

Split from :mod:`zeroth.governance.audit.delivery` along the seam that matters
operationally: that module is what a *producer* touches -- a bounded, synchronous
hand-off and a bounded shutdown -- while everything here runs on the worker,
after the producer is long gone, and is allowed to be slow.

**The invariant: one dequeued event leaves this loop exactly once, in exactly
one counted state, and the loop survives it either way.** The three ways an
event can end -- delivered, already durable, failed -- are each counted, and
nothing else can end the loop. An exception escaping the body used to kill the
only worker, which stranded every later event in the queue *and* left the one
that caused it counted nowhere; the broad guard below is what makes "one bad
event" cost one record instead of the whole stage.

**This loop does not classify, and deliberately so.** It used to apply the
capture policy between the dequeue and the first write, which meant capture ran
twice -- here and again inside
:meth:`~zeroth.governance.audit.repository.AuditRepository.write` -- and the
second pass had to be told the first had happened. The only channel available
for telling it was producer-supplied metadata, so "already captured" was
forgeable and capture was skippable. Classification now belongs to the durable
sink alone; the writer this loop holds must be one (see
:class:`~zeroth.governance.audit.delivery_state.AuditRecordWriter`). What is
left here is delivery and retry.

**Every attempt is bounded, and the bound does not depend on the writer's
manners.** ``await writer.write(...)`` was unbounded, so a single hung write
owned the only worker indefinitely: it never retried, never failed, and showed
up nowhere until the queue filled or shutdown began. Each attempt now runs as
its own task under a finite deadline, and an attempt that overruns it is
cancelled and *left*, never awaited -- a writer that swallows cancellation
cannot extend the bound by ignoring it. The same ``audit_id`` is retried, so if
the abandoned attempt does eventually commit, the retry meets
:class:`~zeroth.governance.audit.errors.DuplicateAuditIdError` and is counted
once, as delivered.

**Released is not forgotten.** An abandoned attempt is never *waited on*, but
its eventual result is still consumed, because the two ways it can end are both
consequential. A *final* attempt that overran the deadline is counted as a
failure and its record may nonetheless commit seconds later -- an operator told
``failed=1`` would go recovering a record that already exists -- so a late
commit reconciles the event, exactly once. And an abandoned attempt that raises
has nobody left to catch it: an unretrieved task exception reaches asyncio's
default handler, which prints the writer's own message and traceback into the
log stream. Both ends are handled by the same completion callback, which logs a
fixed code and an exception *type* and never the message.

**One event, one terminal count.** Shutdown can mark an in-flight event
abandoned while its write is still outstanding, and that write can still
succeed. Claiming the event's terminal state
(:class:`~zeroth.governance.audit.delivery_state.TerminalState`) is what keeps
"abandoned" and "delivered" exclusive; a commit that arrives after the claim is
counted separately, as a reconciliation, so neither number lies.
"""

from __future__ import annotations

import asyncio
import logging
import random
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from zeroth.governance.audit.delivery_state import (
    DeliveryFailure,
    DeliveryOutcome,
    PendingAudit,
)
from zeroth.governance.audit.errors import DuplicateAuditIdError

if TYPE_CHECKING:
    from zeroth.governance.audit.delivery_state import (
        AuditRecordWriter,
        DeliveryCounters,
    )
    from zeroth.governance.audit.models import NodeAuditRecord

logger = logging.getLogger(__name__)

# Long enough that a contended database transaction is not mistaken for a wedge,
# short enough that a wedge is visible well before a queue of 1024 fills.
DEFAULT_WRITE_TIMEOUT_SECONDS = 10.0


class AttemptOutcome(StrEnum):
    """How one bounded write attempt ended."""

    WRITTEN = "written"
    DUPLICATE = "duplicate"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class DeliveryWorker:
    """Consume one bounded queue and write each event with retries.

    Args:
        writer: The durable append-only write. Must be a capture-applying sink;
            this loop hands it the record exactly as the producer built it.
        counters: Where every outcome is recorded.
        queue: The bounded hand-off this loop drains.
        max_attempts: Write attempts per event, including the first.
        base_delay: Backoff base for the second attempt onward.
        max_delay: Ceiling the jittered backoff cannot exceed.
        write_timeout: Finite deadline for one write attempt.
    """

    def __init__(
        self,
        *,
        writer: AuditRecordWriter,
        counters: DeliveryCounters,
        queue: asyncio.Queue[PendingAudit],
        max_attempts: int,
        base_delay: float,
        max_delay: float,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._writer = writer
        self._counters = counters
        self._queue = queue
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._write_timeout = write_timeout
        self._in_flight: PendingAudit | None = None
        self._started_at: float | None = None
        # Attempts that overran their deadline. Owned so they are neither
        # garbage-collected mid-flight nor waited on -- and each one carries the
        # event it was writing, so its eventual result can still be accounted.
        self._orphans: set[asyncio.Task[object]] = set()

    @property
    def in_flight(self) -> PendingAudit | None:
        """The event currently being captured or written, if any."""
        return self._in_flight

    @property
    def in_flight_seconds(self) -> float | None:
        """How long the current event has been in the worker's hands, if any."""
        if self._started_at is None:
            return None
        return max(0.0, asyncio.get_running_loop().time() - self._started_at)

    async def run(self) -> None:
        """Deliver queued events one at a time until cancelled."""
        while True:
            item = await self._queue.get()
            self._in_flight = item
            self._started_at = asyncio.get_running_loop().time()
            try:
                await self._deliver(item, item.record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one event must never take the
                # only worker with it. A crash escaping here left every later
                # event queued forever and uncounted, and re-raised out of the
                # stage's ``aclose`` with the failure recorded nowhere.
                self.fail(item, DeliveryFailure.WORKER_ERROR, exc)
            finally:
                self._in_flight = None
                self._started_at = None
                # The event has left the worker's hands, so nothing is in
                # flight. Left unreset, the gauge kept presenting the last
                # timed-out attempt's duration as the current age of an idle
                # worker's write -- a wedge that had already resolved.
                self._counters.publish_in_flight_age(0.0)
                self._queue.task_done()
                self._counters.publish_depth(self._queue.qsize())

    def fail(
        self, item: PendingAudit, code: DeliveryFailure, exc: BaseException | None = None
    ) -> None:
        """Count one permanently undelivered event, unless shutdown already counted it."""
        if not item.terminal.claim(DeliveryOutcome.FAILED):
            return
        self._counters.record_failure(item.audit_id)
        logger.warning(
            "audit delivery failed code=%s audit_id=%s exception_type=%s",
            code.value,
            item.audit_id,
            "none" if exc is None else type(exc).__name__,
        )

    def commit(self, item: PendingAudit) -> None:
        """Count one durable event, or reconcile a write that landed after the stage gave up."""
        if item.terminal.claim(DeliveryOutcome.DELIVERED):
            self._counters.increment("delivered")
            return
        if not item.terminal.claim_reconciliation():
            # Either the event is already counted as delivered -- a released
            # attempt and its retry both observing the one durable row is one
            # event, not two -- or a previous late commit already reconciled it.
            return
        self._counters.increment("reconciled")
        logger.warning(
            "audit delivery late commit audit_id=%s already counted as %s",
            item.audit_id,
            item.terminal.outcome,
        )

    async def _deliver(self, item: PendingAudit, record: NodeAuditRecord) -> None:
        """Write one event, retrying the same ``audit_id`` until attempts run out."""
        attempt = 1
        while True:
            outcome, exc = await self._attempt(item, record)
            if outcome is AttemptOutcome.WRITTEN or outcome is AttemptOutcome.DUPLICATE:
                # DUPLICATE is the append-only contract's one benign refusal: this
                # audit_id is already stored, so an earlier attempt did land.
                # Deliberately narrow -- every other error, plain ``ValueError``
                # included, means no row was written.
                self.commit(item)
                return
            if attempt >= self._max_attempts:
                code = (
                    DeliveryFailure.WRITE_TIMEOUT
                    if outcome is AttemptOutcome.TIMED_OUT
                    else DeliveryFailure.WRITE_FAILED
                )
                self.fail(item, code, exc)
                return
            self._counters.increment("retried")
            await asyncio.sleep(self._backoff_delay(attempt))
            attempt += 1

    async def _attempt(
        self, item: PendingAudit, record: NodeAuditRecord
    ) -> tuple[AttemptOutcome, BaseException | None]:
        """Run one write under a finite deadline, waiting on nothing past it."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        task: asyncio.Task[object] = asyncio.create_task(
            self._writer.write(record), name="audit-delivery-write"
        )
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._write_timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            # Cancelled and released, never awaited: a writer that swallows the
            # cancellation would otherwise own this worker for as long as it liked.
            # Its result is still consumed, off this stack, by ``_orphan_done``.
            task.cancel()
            self._orphans.add(task)
            task.add_done_callback(partial(self._orphan_done, item))
            self._counters.publish_in_flight_age(loop.time() - started)
            return AttemptOutcome.TIMED_OUT, None
        if task.cancelled():
            return AttemptOutcome.FAILED, None
        exc = task.exception()
        if exc is None:
            return AttemptOutcome.WRITTEN, None
        if isinstance(exc, DuplicateAuditIdError):
            return AttemptOutcome.DUPLICATE, exc
        if not isinstance(exc, Exception):  # pragma: no cover - BaseException escape
            raise exc
        # The writer is injected, so any non-duplicate failure (locked database,
        # disk, a transport under a different implementation) is transient until
        # the attempt budget says otherwise.
        return AttemptOutcome.FAILED, exc

    def _orphan_done(self, item: PendingAudit, task: asyncio.Task[object]) -> None:
        """Account for an attempt that was released at its deadline and finished anyway.

        Runs on the loop, on nobody's stack: this is the only place an abandoned
        attempt's result is ever observed. Returning normally means the record is
        durable -- that is what the writer contract makes a return mean -- and a
        duplicate id means the same thing, so both reconcile the event. Anything
        else is logged by fixed code and exception *type*: an unretrieved task
        exception is what put the writer's own message, and a full traceback,
        into the log stream at asyncio's discretion.

        Args:
            item: The event this attempt was writing, carried since the deadline.
            task: The finished attempt, whose result is consumed here.
        """
        self._orphans.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or isinstance(exc, DuplicateAuditIdError):
            self.commit(item)
            return
        logger.warning(
            "audit delivery abandoned attempt failed code=%s audit_id=%s exception_type=%s",
            DeliveryFailure.ABANDONED_WRITE_FAILED.value,
            item.audit_id,
            type(exc).__name__,
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff for a 1-based attempt number."""
        ceiling = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
        return random.uniform(0.0, ceiling)  # noqa: S311 - backoff jitter, not crypto


__all__ = ["DEFAULT_WRITE_TIMEOUT_SECONDS", "AttemptOutcome", "DeliveryWorker"]

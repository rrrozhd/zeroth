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
        # garbage-collected mid-flight nor waited on.
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
        """Count one durable event, or reconcile a write that landed after shutdown gave up."""
        if item.terminal.claim(DeliveryOutcome.DELIVERED):
            self._counters.increment("delivered")
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
            outcome, exc = await self._attempt(record)
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
        self, record: NodeAuditRecord
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
            task.cancel()
            self._orphans.add(task)
            task.add_done_callback(self._orphans.discard)
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

    def _backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff for a 1-based attempt number."""
        ceiling = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
        return random.uniform(0.0, ceiling)  # noqa: S311 - backoff jitter, not crypto


__all__ = ["DEFAULT_WRITE_TIMEOUT_SECONDS", "AttemptOutcome", "DeliveryWorker"]

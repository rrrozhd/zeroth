"""Bounded, non-blocking delivery of audit events into the durable audit write.

This module owns the hand-off between an audit-event producer -- a request
handler, a gateway event sink, a node runner -- and the append-only audit
repository. The producer sits on a latency-sensitive path; the write is a
locked database transaction that can be slow and can fail transiently. Every
rule below exists to keep those two facts from colliding.

**The invariant: one submitted event becomes at most one persisted record, and
never suspends its producer.** :meth:`AuditDeliveryQueue.submit` is a plain
synchronous method -- deliberately *not* a coroutine -- that performs a single
``put_nowait`` onto a finite :class:`asyncio.Queue` and returns a ``bool``.
There is no ``await queue.put(...)`` anywhere in this module, so no producer
can be parked behind a wedged audit writer, and the queue cannot grow past
``max_queue_size``.

**Saturation is reject-newest, and it is counted.** When the queue is full the
*newest* event is rejected: ``submit`` returns ``False`` and the rejection is
counted under ``zeroth_audit_delivery_rejected_total``. Drop-oldest -- the
idiom the econ telemetry transport uses for throughput samples -- is wrong for
audit evidence, because it discards the earliest record in the window (the one
an investigation reads first) and tells nobody: the producer of the discarded
event returned successfully long ago. Reject-newest hands the loss back to the
only caller still on the stack, which can log, degrade, or refuse.

**Retries reuse the ``audit_id`` minted at submit time.** The identity is fixed
by the producer before ``submit`` and carried unchanged on the frozen queue
item, so every attempt writes the same one -- which is what makes a retry
idempotent. ``AuditRepository.write`` raises
:class:`~zeroth.governance.audit.errors.DuplicateAuditIdError` when that id is
already stored, so a retry following a partially-succeeded write finds the
record durable, and this stage counts *that specific type* as **delivered**.
Nothing wider: the same method raises plain ``ValueError`` for a record it
rejected before the commit, and reading every ``ValueError`` as "already
stored" reported a record as delivered that was never written at all.

**Redaction is owned here and cannot be swapped.** The queue constructs its own
:class:`~zeroth.governance.audit.capture_policy.AuditCapturePolicy` and applies
it between the dequeue and the first write attempt; no parameter accepts a
policy object. A caller may inject a *classifier* -- which picks between two
fixed outcomes -- and may widen the redaction rules, but it cannot supply the
transform, because a boundary a caller can replace with a pass-through is not a
boundary: the previous shape accepted one and wrote the producer's prompt
verbatim. If the policy fails while failing closed the worker falls back to
:func:`~zeroth.governance.audit.capture_policy.blank_record`, and failing even
that it drops the event as ``failed`` rather than letting one exception take
the only worker with it. Running the transform once, ahead of the retry loop,
is what makes "no attempt can reach the writer with what the producer
submitted" true without walking the same payload per attempt.

**Exhaustion is failure, not delivery, and failure keeps its name.** After the
final attempt an event is counted as ``failed`` and its ``audit_id`` retained,
so :meth:`AuditDeliveryQueue.aclose` can report it: a shutdown returning
``drained=True`` while a record had been dropped hours earlier told an operator
the stage had lost nothing.

**Shutdown runs once, under one absolute deadline.** Concurrent ``aclose``
callers share a single close task and its single report; independent snapshots
let two callers abandon and count the same in-flight event twice. The deadline
spans the drain *and* the worker cancellation, because a writer that swallows
:class:`asyncio.CancelledError` otherwise keeps the close running for as long
as it likes -- such a worker is left running and reported as an undrained
close, never waited on past the bound.

Delivery state lives on the queue item, never on ``NodeAuditRecord``: that
model is ``extra="forbid"`` and every one of its fields feeds the per-run
digest chain, so an attempt counter stored there would rewrite the digest of
every record ever written.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.governance.audit.delivery_state import (
    COUNTER_METRICS,
    IN_FLIGHT_AGE_GAUGE,
    QUEUE_DEPTH_GAUGE,
    AuditDeliveryCounts,
    AuditDeliveryReport,
    AuditRecordWriter,
    DeliveryCounters,
    DeliveryFailure,
    DeliveryOutcome,
    DeliveryRejection,
    PendingAudit,
    validate_seconds,
)
from zeroth.governance.audit.delivery_worker import (
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    DeliveryWorker,
)
from zeroth.platform.observability.metrics import MetricsCollector

if TYPE_CHECKING:
    from collections.abc import Mapping

    from zeroth.governance.audit.capture_policy import CaptureClassifier
    from zeroth.governance.audit.models import AuditRedactionConfig, NodeAuditRecord

logger = logging.getLogger(__name__)


class AuditDeliveryQueue:
    """Bounded delivery stage between an audit producer and the durable write.

    A single worker task consumes the queue, applies this stage's own capture
    policy, and writes one event at a time -- retrying the *same* record, and
    therefore the same ``audit_id``, with jittered exponential backoff until it
    lands or the attempt budget runs out.

    Args:
        writer: The durable append-only write. Must be cancellable; see
            :class:`~zeroth.governance.audit.delivery_state.AuditRecordWriter`.
        max_queue_size: How many events may wait before ``submit`` rejects.
        max_attempts: Write attempts per event, including the first.
        base_delay_seconds: Backoff base for the second attempt onward.
        max_delay_seconds: Ceiling the jittered backoff cannot exceed.
        write_timeout_seconds: Finite deadline for one write attempt. An
            attempt that overruns it is cancelled, counted, and retried under
            the same ``audit_id``; it is never waited on past the bound.
        metrics: Where the counters are published.
        classifier: Decides per event whether content may be retained. The one
            replaceable part of the capture boundary, and it chooses between
            two fixed outcomes rather than authoring the record.
        redaction: Extra key-redaction and path-omission rules. Widens the
            stage's defaults; cannot narrow them.
        known_secrets: Resolved secret values to mask wherever they appear.
    """

    def __init__(
        self,
        writer: AuditRecordWriter,
        *,
        max_queue_size: int = 1024,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 30.0,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        metrics: MetricsCollector | None = None,
        classifier: CaptureClassifier | None = None,
        redaction: AuditRedactionConfig | None = None,
        known_secrets: Mapping[str, str] | None = None,
    ) -> None:
        if type(max_queue_size) is not int or max_queue_size < 1:
            raise ValueError("max_queue_size must be a positive int")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive int")
        validate_seconds("base_delay_seconds", base_delay_seconds)
        validate_seconds("max_delay_seconds", max_delay_seconds)
        validate_seconds("write_timeout_seconds", write_timeout_seconds)
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below base_delay_seconds")
        if write_timeout_seconds <= 0:
            raise ValueError("write_timeout_seconds must be positive")
        # A private collector rather than None: metrics are always recorded, and
        # an un-wired deployment loses the scrape, not the accounting.
        self._counters = DeliveryCounters(
            MetricsCollector() if metrics is None else metrics,
            max_retained_ids=max_queue_size,
        )
        self._queue: asyncio.Queue[PendingAudit] = asyncio.Queue(maxsize=max_queue_size)
        self._loop_worker = DeliveryWorker(
            writer=writer,
            # Constructed, never accepted: see the module docstring. Only the
            # classifier and the redaction inputs cross this boundary.
            policy=AuditCapturePolicy(
                classifier=classifier, redaction=redaction, known_secrets=known_secrets
            ),
            counters=self._counters,
            queue=self._queue,
            max_attempts=max_attempts,
            base_delay=float(base_delay_seconds),
            max_delay=float(max_delay_seconds),
            write_timeout=float(write_timeout_seconds),
        )
        self._tasks: set[asyncio.Task[Any]] = set()
        self._worker: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[AuditDeliveryReport] | None = None
        self._closed = False

    @property
    def pending(self) -> int:
        """How many events are queued but not yet picked up by the worker."""
        return self._queue.qsize()

    @property
    def in_flight_seconds(self) -> float | None:
        """How long the event the worker holds has been outstanding, if any.

        The only signal that distinguishes a wedged stage from an idle one:
        a hung write moves no counter at all until the queue fills.
        """
        return self._loop_worker.in_flight_seconds

    def counts(self) -> AuditDeliveryCounts:
        """Return an immutable snapshot of the delivery counters."""
        return self._counters.snapshot()

    def start(self) -> None:
        """Ensure the single delivery worker task is running.

        Idempotent, and called automatically by :meth:`submit`. Requires a
        running event loop.
        """
        if self._closed:
            return
        if self._worker is not None and not self._worker.done():
            return
        worker = asyncio.create_task(self._loop_worker.run(), name="audit-delivery")
        self._worker = worker
        self._track(worker)

    def submit(self, record: NodeAuditRecord) -> bool:
        """Hand one audit record to the delivery stage without ever blocking.

        Args:
            record: The record to persist. Its ``audit_id`` is minted by the
                caller, once, and is reused unchanged by every retry.

        Returns:
            ``True`` when the record was queued; ``False`` when it was rejected
            because the queue is full or the stage is closed. A ``False`` is
            counted and is the caller's to handle -- it is the only signal that
            this event will not be persisted.

        Raises:
            ValueError: If ``record.audit_id`` or ``record.tenant_id`` is not a
                non-empty string once stripped. Counted as an ``invalid_record``
                rejection *before* it is raised: the refusal used to move no
                counter at all, so an event with a blank tenant disappeared while
                ``/health`` and ``/v1/metrics`` still reported nothing lost. A
                whitespace-only tenant was accepted outright, and "  " is not a
                tenant -- it is a record that will be attributed to nobody.
        """
        audit_id = record.audit_id
        for name, value in (("audit_id", audit_id), ("tenant_id", record.tenant_id)):
            if type(value) is not str or not value.strip():
                self._counters.increment("rejected", reason=DeliveryRejection.INVALID_RECORD)
                logger.warning("audit delivery rejected an invalid %s", name)
                raise ValueError(f"record.{name} must be a non-empty str")
        if self._closed:
            self._counters.increment("rejected", reason=DeliveryRejection.CLOSED)
            return False
        try:
            self._queue.put_nowait(PendingAudit(audit_id=audit_id, record=record))
        except asyncio.QueueFull:
            self._counters.increment("rejected", reason=DeliveryRejection.QUEUE_FULL)
            logger.warning("audit delivery queue full; rejected audit_id %s", audit_id)
            return False
        self._counters.increment("queued")
        self._counters.publish_depth(self._queue.qsize())
        self.start()
        return True

    def reject(self, audit_id: str, reason: DeliveryRejection) -> None:
        """Count one event that never reached the queue, so the loss is still visible.

        A producer can fail *before* ``submit`` -- a terminal gateway event that
        cannot be projected into a record at all -- and that loss used to reach
        nothing but a log line. Routing it through this stage's counters is what
        puts it on ``/v1/metrics`` and in the readiness probe beside every other
        way an event fails to become durable.

        Args:
            audit_id: The identity the producer had minted, for the log line.
            reason: Why the event never became a queued record.
        """
        self._counters.increment("rejected", reason=reason)
        logger.warning("audit delivery rejected audit_id=%s reason=%s", audit_id, reason.value)

    async def aclose(self, *, timeout: float = 5.0) -> AuditDeliveryReport:
        """Stop accepting events, drain what is in flight, and report the rest.

        Runs at most once. Concurrent and later callers await the same close
        task and receive the same report, because two independent closes would
        each abandon -- and each count -- the one event that was mid-write.

        Args:
            timeout: Seconds the whole shutdown may take, spanning the drain
                and the worker's cancellation. Honoured from the first caller;
                a later caller waits for the close already in progress.

        Returns:
            A report naming every event that could not be persisted, whether it
            exhausted its retries or was abandoned at the bound. Nothing is lost
            silently: those same events are counted.
        """
        validate_seconds("timeout", timeout)
        self._closed = True
        if self._close_task is None:
            # There is no await between the check and the assignment, so this is
            # the whole of the mutual exclusion a lock would buy here.
            self._close_task = asyncio.create_task(
                self._drain(timeout), name="audit-delivery-close"
            )
            self._track(self._close_task)
        return await asyncio.shield(self._close_task)

    async def _drain(self, timeout: float) -> AuditDeliveryReport:
        """Resolve every outstanding event, or name it, within one absolute deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        worker = self._worker
        drained = True
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(self._queue.join(), timeout)
            except TimeoutError:
                drained = False
        # Snapshot the abandoned work BEFORE cancelling: the worker clears its
        # in-flight slot inside a finally, so cancelling first would erase the
        # identity of the one event that was mid-write.
        undelivered = self._abandon_remaining()
        if worker is not None:
            if not worker.done():
                worker.cancel()
                done, _pending = await asyncio.wait(
                    {worker}, timeout=max(0.0, deadline - loop.time())
                )
                if not done:
                    # A writer that swallowed the cancellation. The task stays
                    # owned by ``_tasks`` and is reported, never waited on past
                    # the bound the caller asked for.
                    drained = False
            if worker.done():
                self._worker = None
        self._counters.publish_depth(self._queue.qsize())
        failed = self._counters.failed_audit_ids
        return AuditDeliveryReport(
            drained=drained and not failed,
            undelivered_audit_ids=(*failed, *undelivered),
            counts=self.counts(),
        )

    def _abandon_remaining(self) -> tuple[str, ...]:
        """Count and name every event still undelivered once the drain bound expired.

        Abandonment is *claimed* on each event, not merely counted: the in-flight
        write is still outstanding here, and a writer that ignores the coming
        cancellation can return successfully afterwards. Without the claim that
        event was counted as abandoned *and* delivered -- one event, two
        mutually exclusive outcomes. A commit after the claim is counted as a
        reconciliation instead.
        """
        items: list[PendingAudit] = []
        in_flight = self._loop_worker.in_flight
        if in_flight is not None:
            items.append(in_flight)
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            items.append(item)
            self._queue.task_done()
        abandoned: list[str] = []
        for item in items:
            if not item.terminal.claim(DeliveryOutcome.ABANDONED):
                continue
            abandoned.append(item.audit_id)
            self._counters.increment("abandoned")
            logger.warning("audit delivery abandoned audit_id %s at shutdown", item.audit_id)
        return tuple(abandoned)

    def _track(self, task: asyncio.Task[Any]) -> None:
        """Own a task for its lifetime, so it is neither collected nor silently lost."""
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Retire a supervised task, retrieving the exception the set would have swallowed."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "audit delivery task error code=%s task=%s exception_type=%s",
                DeliveryFailure.WORKER_ERROR.value,
                task.get_name(),
                type(exc).__name__,
            )


__all__ = [
    "COUNTER_METRICS",
    "IN_FLIGHT_AGE_GAUGE",
    "QUEUE_DEPTH_GAUGE",
    "AuditDeliveryCounts",
    "AuditDeliveryQueue",
    "AuditDeliveryReport",
    "AuditRecordWriter",
    "DeliveryFailure",
    "DeliveryOutcome",
    "DeliveryRejection",
]

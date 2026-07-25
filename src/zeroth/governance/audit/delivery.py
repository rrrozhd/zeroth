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

**Retries reuse the ``audit_id`` minted at submit time; they never mint a new
one.** The identity is fixed by the producer before ``submit`` and is carried
unchanged on the frozen queue item, so every attempt writes the same identity.
That is what makes a retry idempotent here. ``AuditRepository.write`` is
append-only and raises :class:`ValueError` when an ``audit_id`` already exists,
so a retry that follows a partially-succeeded write finds the record already
durable -- and this stage counts that as **delivered**, not as an error.
Re-minting an id per attempt (what a naive retry wrapped around the existing
gateway sink would do, since it calls ``uuid4()`` on every emit) would persist
one logical event as two records.

**A queued event carries its own tenant.** The worker runs long after the
producer's request context is gone, so the tenant must ride on the record --
and ``NodeAuditRecord.tenant_id`` defaults to ``"default"``, the reserved tenant
owning the fallback retention policy, so an absent one is misattributed *and*
given the wrong TTL, silently on both counts. ``submit`` therefore rejects a
blank tenant: the one shape of that omission pydantic has not already erased.

**Capture classification and redaction run on the worker, once per event, ahead
of the first attempt.** The transform is
:class:`~zeroth.governance.audit.capture_policy.AuditCapturePolicy`, and this
stage applies it *itself* rather than trusting a producer to have done so.
``_consume`` runs it between the dequeue and ``_deliver``, so ``_deliver`` --
the only code here that touches the writer -- holds nothing but the captured
record. Producer-side application was the alternative: it would keep an
unredacted record out of the in-process buffer, but it would also put an
O(payload) walk on the latency-sensitive path that ``submit`` exists to keep
O(1), and the buffer holds an object the producer was already holding, whereas
the writer is durable storage. Running it once, ahead of the retry loop, is what
makes "no attempt -- including the one after a partial success -- can reach the
writer with what the producer submitted" true without walking the same payload
once per attempt and risking a doubly-escaped value.

**Exhaustion is failure, not delivery.** After the final attempt an event is
counted as ``failed`` and dropped. ``failed``, ``rejected`` and ``delivered``
are distinct counters precisely so an operator can tell "the writer is broken"
from "the producer outran the writer" -- collapsing them would make the
delivery stage look healthy in exactly the two cases where it is not.

Delivery state lives on the queue item, never on ``NodeAuditRecord``: that
model is ``extra="forbid"`` and every one of its fields feeds the per-run
digest chain, so an attempt counter stored there would rewrite the digest of
every record ever written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.platform.observability.metrics import MetricsCollector

if TYPE_CHECKING:
    from zeroth.governance.audit.models import NodeAuditRecord

logger = logging.getLogger(__name__)

QUEUE_DEPTH_GAUGE = "zeroth_audit_delivery_queue_depth"

_COUNTER_METRICS = {
    "queued": "zeroth_audit_delivery_queued_total",
    "delivered": "zeroth_audit_delivery_delivered_total",
    "retried": "zeroth_audit_delivery_retried_total",
    "rejected": "zeroth_audit_delivery_rejected_total",
    "failed": "zeroth_audit_delivery_failed_total",
    "abandoned": "zeroth_audit_delivery_abandoned_total",
}


class AuditRecordWriter(Protocol):
    """The durable audit write this stage delivers into.

    Satisfied by :class:`~zeroth.governance.audit.repository.AuditRepository`.
    One contract term matters to this module: the write is append-only and
    raises :class:`ValueError` *only* when the record's ``audit_id`` is already
    stored, which this stage reads as "already durable".
    """

    async def write(self, record: NodeAuditRecord) -> object:
        """Persist one audit record, raising ``ValueError`` on a duplicate id."""
        ...


class DeliveryRejection(StrEnum):
    """Why :meth:`AuditDeliveryQueue.submit` refused an event."""

    QUEUE_FULL = "queue_full"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _PendingAudit:
    """One queued event, carrying the identity every retry reuses."""

    audit_id: str
    record: NodeAuditRecord


@dataclass(frozen=True, slots=True)
class AuditDeliveryCounts:
    """Immutable snapshot of the delivery counters."""

    queued: int = 0
    delivered: int = 0
    retried: int = 0
    rejected: int = 0
    failed: int = 0
    abandoned: int = 0


@dataclass(frozen=True, slots=True)
class AuditDeliveryReport:
    """Outcome of a graceful shutdown.

    Attributes:
        drained: ``True`` when every queued event was resolved within the
            shutdown bound.
        undelivered_audit_ids: The ids this stage could not persist. Empty
            whenever ``drained`` is ``True``.
        counts: The counter snapshot taken after the drain.
    """

    drained: bool
    undelivered_audit_ids: tuple[str, ...]
    counts: AuditDeliveryCounts


def _validate_seconds(name: str, value: float) -> None:
    """Reject a delay that is not a non-negative real number of seconds."""
    if type(value) is not float and type(value) is not int:
        raise ValueError(f"{name} must be a real number of seconds")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


class AuditDeliveryQueue:
    """Bounded delivery stage between an audit producer and the durable write.

    A single worker task consumes the queue and writes one event at a time,
    retrying the *same* record -- and therefore the same ``audit_id`` -- with
    jittered exponential backoff until it lands or the attempt budget runs out.
    """

    def __init__(
        self,
        writer: AuditRecordWriter,
        *,
        max_queue_size: int = 1024,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 30.0,
        metrics: MetricsCollector | None = None,
        capture_policy: AuditCapturePolicy | None = None,
    ) -> None:
        if type(max_queue_size) is not int or max_queue_size < 1:
            raise ValueError("max_queue_size must be a positive int")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive int")
        _validate_seconds("base_delay_seconds", base_delay_seconds)
        _validate_seconds("max_delay_seconds", max_delay_seconds)
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below base_delay_seconds")
        self._writer = writer
        self._max_attempts = max_attempts
        self._base_delay = float(base_delay_seconds)
        self._max_delay = float(max_delay_seconds)
        # A private collector rather than None: metrics are always recorded, and
        # an un-wired deployment loses the scrape, not the accounting.
        self._metrics = MetricsCollector() if metrics is None else metrics
        # Never None, for the same reason the collector is not: "no policy was
        # supplied" must not resolve to "persist what the producer sent".
        self._policy = AuditCapturePolicy() if capture_policy is None else capture_policy
        self._queue: asyncio.Queue[_PendingAudit] = asyncio.Queue(maxsize=max_queue_size)
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker: asyncio.Task[None] | None = None
        self._in_flight: _PendingAudit | None = None
        self._closed = False
        self._counts = dict.fromkeys(_COUNTER_METRICS, 0)

    @property
    def pending(self) -> int:
        """How many events are queued but not yet picked up by the worker."""
        return self._queue.qsize()

    def counts(self) -> AuditDeliveryCounts:
        """Return an immutable snapshot of the delivery counters."""
        return AuditDeliveryCounts(**self._counts)

    def start(self) -> None:
        """Ensure the single delivery worker task is running.

        Idempotent, and called automatically by :meth:`submit`. Requires a
        running event loop.
        """
        if self._closed:
            return
        if self._worker is not None and not self._worker.done():
            return
        worker = asyncio.create_task(self._consume(), name="audit-delivery")
        self._worker = worker
        self._tasks.add(worker)
        worker.add_done_callback(self._tasks.discard)

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
                non-empty string.
        """
        audit_id = record.audit_id
        for name, value in (("audit_id", audit_id), ("tenant_id", record.tenant_id)):
            if type(value) is not str or not value:
                raise ValueError(f"record.{name} must be a non-empty str")
        if self._closed:
            self._count("rejected", reason=DeliveryRejection.CLOSED)
            return False
        try:
            self._queue.put_nowait(_PendingAudit(audit_id=audit_id, record=record))
        except asyncio.QueueFull:
            self._count("rejected", reason=DeliveryRejection.QUEUE_FULL)
            logger.warning("audit delivery queue full; rejected audit_id %s", audit_id)
            return False
        self._count("queued")
        self._publish_depth()
        self.start()
        return True

    async def aclose(self, *, timeout: float = 5.0) -> AuditDeliveryReport:
        """Stop accepting events, drain what is in flight, and report the rest.

        Args:
            timeout: Seconds to spend draining before abandoning the remainder.

        Returns:
            A report naming every event that could not be persisted before the
            bound expired. Nothing is lost silently: those same events are
            counted as abandoned.
        """
        _validate_seconds("timeout", timeout)
        self._closed = True
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
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._worker = None
        self._publish_depth()
        return AuditDeliveryReport(
            drained=drained,
            undelivered_audit_ids=undelivered,
            counts=self.counts(),
        )

    async def _consume(self) -> None:
        """Deliver queued events one at a time until cancelled."""
        while True:
            item = await self._queue.get()
            self._in_flight = item
            try:
                # The single application point of the capture policy: everything
                # downstream of here, retries included, sees only its output.
                captured = _PendingAudit(
                    audit_id=item.audit_id, record=self._policy.apply(item.record)
                )
                await self._deliver(captured)
            finally:
                self._in_flight = None
                self._queue.task_done()
                self._publish_depth()

    async def _deliver(self, item: _PendingAudit) -> None:
        """Write one event, retrying the same ``audit_id`` until attempts run out."""
        attempt = 1
        while True:
            try:
                await self._writer.write(item.record)
            except asyncio.CancelledError:
                raise
            except ValueError:
                # Append-only contract: a ValueError means this audit_id is
                # already stored, so an earlier attempt did land. The event is
                # durable -- counting it as failed would report a loss that
                # never happened, and re-writing it is impossible by design.
                self._count("delivered")
                return
            except Exception as exc:  # noqa: BLE001 - the writer is injected, so any
                # non-duplicate failure (locked database, disk, a transport under
                # a different implementation) is transient until the attempt
                # budget says otherwise. Narrowing this would let an
                # unanticipated storage error kill the worker for every later
                # event, which is a much larger loss than one dropped record.
                if attempt >= self._max_attempts:
                    self._count("failed")
                    logger.warning(
                        "audit delivery failed after %d attempts for audit_id %s: %s",
                        attempt,
                        item.audit_id,
                        exc,
                    )
                    return
                self._count("retried")
                await asyncio.sleep(self._backoff_delay(attempt))
                attempt += 1
            else:
                self._count("delivered")
                return

    def _backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff for a 1-based attempt number."""
        ceiling = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
        return random.uniform(0.0, ceiling)  # noqa: S311 - backoff jitter, not crypto

    def _abandon_remaining(self) -> tuple[str, ...]:
        """Count and name every event still undelivered once the drain bound expired."""
        pending: list[str] = []
        if self._in_flight is not None:
            pending.append(self._in_flight.audit_id)
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            pending.append(item.audit_id)
            self._queue.task_done()
        for audit_id in pending:
            self._count("abandoned")
            logger.warning("audit delivery abandoned audit_id %s at shutdown", audit_id)
        return tuple(pending)

    def _count(self, field: str, *, reason: DeliveryRejection | None = None) -> None:
        """Increment one counter locally and on the injected metrics collector."""
        self._counts[field] += 1
        labels = None if reason is None else {"reason": reason.value}
        self._metrics.increment(_COUNTER_METRICS[field], labels)

    def _publish_depth(self) -> None:
        """Publish the current queue depth as a gauge."""
        self._metrics.gauge_set(QUEUE_DEPTH_GAUGE, float(self._queue.qsize()))


__all__ = [
    "QUEUE_DEPTH_GAUGE",
    "AuditDeliveryCounts",
    "AuditDeliveryQueue",
    "AuditDeliveryReport",
    "AuditRecordWriter",
    "DeliveryRejection",
]

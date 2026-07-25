"""The delivery stage's writer contract, counters and shutdown report.

Split out of :mod:`zeroth.governance.audit.delivery` so that module holds one
thing -- the worker loop -- and so the accounting has a home of its own. The
accounting is not incidental here: an audit delivery stage whose counters are
approximate is a stage that can report health while losing evidence, so
"delivered", "failed", "rejected" and "abandoned" are four distinct outcomes
that never substitute for one another.

**Identity outlives the counter.** A failed event is not just a number: the
``audit_id`` is what an operator needs to go and recover the record from
whatever the producer still has. :class:`DeliveryCounters` therefore retains
the ids of events it could not persist, bounded by the same order of magnitude
as the queue itself so a permanently broken writer cannot turn the accounting
into a memory leak. The counter stays exact even when the id list stops
growing, which is the right way round: an operator who sees ``failed`` exceed
the ids they were handed knows the list is a floor, not a total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from zeroth.platform.observability.metrics import MetricsCollector

if TYPE_CHECKING:
    from zeroth.governance.audit.models import NodeAuditRecord

QUEUE_DEPTH_GAUGE = "zeroth_audit_delivery_queue_depth"
IN_FLIGHT_AGE_GAUGE = "zeroth_audit_delivery_in_flight_seconds"

COUNTER_METRICS = {
    "queued": "zeroth_audit_delivery_queued_total",
    "delivered": "zeroth_audit_delivery_delivered_total",
    "retried": "zeroth_audit_delivery_retried_total",
    "rejected": "zeroth_audit_delivery_rejected_total",
    "failed": "zeroth_audit_delivery_failed_total",
    "abandoned": "zeroth_audit_delivery_abandoned_total",
    # Not a loss and not a delivery: an event shutdown already counted as
    # abandoned, whose write turned out to land afterwards. Counted apart so
    # "delivered" keeps meaning "durable and accounted exactly once".
    "reconciled": "zeroth_audit_delivery_reconciled_total",
}


class AuditRecordWriter(Protocol):
    """The durable audit write this stage delivers into.

    Satisfied by :class:`~zeroth.governance.audit.repository.AuditRepository`.
    Two contract terms matter to the delivery stage:

    * The write is append-only and raises
      :class:`~zeroth.governance.audit.errors.DuplicateAuditIdError` -- and
      *only* that type -- when the record's ``audit_id`` is already stored,
      which this stage reads as "already durable". Any other failure, including
      any other ``ValueError``, means the record was not written.
    * The write is cancellable: it must let :class:`asyncio.CancelledError`
      propagate rather than swallowing it or shielding itself. Graceful
      shutdown enforces one absolute deadline over both the drain and the
      cancellation, and a writer that ignores cancellation can only be left
      running past it -- reported as an undrained close, never waited on.
    """

    async def write(self, record: NodeAuditRecord) -> object:
        """Persist one audit record, raising ``DuplicateAuditIdError`` on a duplicate id."""
        ...


class DeliveryOutcome(StrEnum):
    """The mutually exclusive terminal states one accepted event can reach."""

    DELIVERED = "delivered"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(slots=True)
class TerminalState:
    """The single terminal outcome one accepted event is counted under.

    Shutdown marks an in-flight event abandoned and then cancels the worker, but
    a writer that swallows :class:`asyncio.CancelledError` can return afterwards
    and report success -- which counted one event as both abandoned and
    delivered, and the two counters are supposed to be exclusive. Claiming is
    what makes them so.
    """

    outcome: DeliveryOutcome | None = None

    def claim(self, outcome: DeliveryOutcome) -> bool:
        """Take the event's one terminal outcome, or report that it is already taken.

        Atomic without a lock: there is no ``await`` between the read and the
        write, so no other coroutine on this loop can interleave between them.
        """
        if self.outcome is not None:
            return False
        self.outcome = outcome
        return True


@dataclass(frozen=True, slots=True)
class PendingAudit:
    """One queued event, carrying the identity every retry reuses."""

    audit_id: str
    record: NodeAuditRecord
    terminal: TerminalState = field(default_factory=TerminalState)


class DeliveryRejection(StrEnum):
    """Why the delivery stage refused an event at submit time."""

    QUEUE_FULL = "queue_full"
    CLOSED = "closed"
    # A record the stage cannot account for: no tenant, or no audit identity.
    # Counted before the refusal is raised, because a rejection nobody counted
    # is an event that vanished while every health surface still read "ok".
    INVALID_RECORD = "invalid_record"
    # A terminal event the gateway sink could not even project into a record.
    PROJECTION_FAILED = "projection_failed"


class DeliveryFailure(StrEnum):
    """Why the delivery stage could not persist an event it had accepted.

    Logged as a fixed code rather than an exception message: the failing code
    is a producer-supplied classifier, an arbitrary payload walker or an
    injected writer, and every one of them can put the value it was holding
    into the text it raises.
    """

    CAPTURE_FAILED = "capture_failed"
    WRITE_FAILED = "write_failed"
    WRITE_TIMEOUT = "write_timeout"
    WORKER_ERROR = "worker_error"


@dataclass(frozen=True, slots=True)
class AuditDeliveryCounts:
    """Immutable snapshot of the delivery counters."""

    queued: int = 0
    delivered: int = 0
    retried: int = 0
    rejected: int = 0
    failed: int = 0
    abandoned: int = 0
    reconciled: int = 0


@dataclass(frozen=True, slots=True)
class AuditDeliveryReport:
    """Outcome of a graceful shutdown.

    Attributes:
        drained: ``True`` only when every accepted event was persisted within
            the shutdown bound. An event that exhausted its retries is not
            drained work -- it is lost work that happens to have finished
            trying.
        undelivered_audit_ids: The ids this stage could not persist, whether
            they failed outright or were abandoned at the bound. Empty whenever
            ``drained`` is ``True``.
        counts: The counter snapshot taken after the drain.
    """

    drained: bool
    undelivered_audit_ids: tuple[str, ...]
    counts: AuditDeliveryCounts


def validate_seconds(name: str, value: float) -> None:
    """Reject a delay that is not a finite, non-negative real number of seconds.

    ``NaN`` compares false against every bound, so an unchecked one slips
    through a ``< 0`` test and then makes ``asyncio.sleep`` and the drain
    deadline undefined; ``inf`` passes every bound honestly and makes the
    "bounded shutdown" claim false. Both are rejected here rather than at the
    two places that consume them.

    Raises:
        ValueError: If ``value`` is not a finite non-negative ``int`` or ``float``.
    """
    if type(value) is not float and type(value) is not int:
        raise ValueError(f"{name} must be a real number of seconds")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


class DeliveryCounters:
    """The stage's counters, the metrics they publish, and the ids it lost.

    Args:
        metrics: Where every increment is mirrored.
        max_retained_ids: How many undelivered ``audit_id`` values to keep for
            the close report. The counters stay exact past this point; only the
            identity list stops growing.
    """

    def __init__(self, metrics: MetricsCollector, *, max_retained_ids: int) -> None:
        self._metrics = metrics
        self._max_retained_ids = max_retained_ids
        self._counts = dict.fromkeys(COUNTER_METRICS, 0)
        self._failed_ids: list[str] = []

    def snapshot(self) -> AuditDeliveryCounts:
        """Return an immutable snapshot of the counters."""
        return AuditDeliveryCounts(**self._counts)

    @property
    def failed_audit_ids(self) -> tuple[str, ...]:
        """The retained ids of events that exhausted their attempts or could not be captured."""
        return tuple(self._failed_ids)

    def increment(self, field: str, *, reason: DeliveryRejection | None = None) -> None:
        """Increment one counter locally and on the metrics collector."""
        self._counts[field] += 1
        labels = None if reason is None else {"reason": reason.value}
        self._metrics.increment(COUNTER_METRICS[field], labels)

    def record_failure(self, audit_id: str) -> None:
        """Count one permanently failed event and retain its id while there is room."""
        self.increment("failed")
        if len(self._failed_ids) < self._max_retained_ids:
            self._failed_ids.append(audit_id)

    def publish_depth(self, depth: int) -> None:
        """Publish the current queue depth as a gauge."""
        self._metrics.gauge_set(QUEUE_DEPTH_GAUGE, float(depth))

    def publish_in_flight_age(self, seconds: float) -> None:
        """Publish how long the current write attempt has been outstanding.

        A wedged writer produces no counter movement at all until the queue
        fills or shutdown begins, so the age is the only signal that the stage
        is stuck rather than idle.
        """
        self._metrics.gauge_set(IN_FLIGHT_AGE_GAUGE, float(seconds))


__all__ = [
    "COUNTER_METRICS",
    "IN_FLIGHT_AGE_GAUGE",
    "QUEUE_DEPTH_GAUGE",
    "AuditDeliveryCounts",
    "AuditDeliveryReport",
    "AuditRecordWriter",
    "DeliveryCounters",
    "DeliveryFailure",
    "DeliveryOutcome",
    "DeliveryRejection",
    "PendingAudit",
    "TerminalState",
    "validate_seconds",
]

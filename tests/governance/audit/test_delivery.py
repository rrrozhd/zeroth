"""The bounded audit-event delivery stage.

``AuditDeliveryQueue`` sits between an audit-event producer and the append-only
audit repository. These tests pin the four properties the stage exists to
provide: the producer is never blocked and the queue is finite (R1), a retried
event persists exactly once (R2), every outcome is counted (R7), and shutdown
drains within a bound and names what it could not deliver (R10).

The writer stubs below imitate the real repository contract deliberately: a
duplicate ``audit_id`` raises ``DuplicateAuditIdError`` -- and *only* a
duplicate does -- which is what makes the idempotency of a retry observable
rather than assumed, and what makes "a validation failure is not a delivery"
testable at all.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

import pytest

from zeroth.governance.audit import delivery_worker as delivery_module
from zeroth.governance.audit.delivery import (
    QUEUE_DEPTH_GAUGE,
    AuditDeliveryQueue,
)
from zeroth.governance.audit.errors import DuplicateAuditIdError
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.platform.observability.metrics import MetricsCollector


class _CollectingAuditWriter:
    """Append-only writer: stores each record, rejects a duplicate audit_id."""

    def __init__(self) -> None:
        self.records: list[NodeAuditRecord] = []
        self.attempted_ids: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempted_ids.append(record.audit_id)
        if any(stored.audit_id == record.audit_id for stored in self.records):
            raise DuplicateAuditIdError(f"audit_id {record.audit_id!r} already exists")
        self.records.append(record)
        return record


class _ValidationRejectingWriter(_CollectingAuditWriter):
    """Raises a plain ``ValueError`` before the commit -- nothing is ever stored."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempted_ids.append(record.audit_id)
        raise ValueError("record failed pre-commit validation")


class _ExplodingCapturePolicy:
    """Stands in for a capture transform that fails while failing closed."""

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        raise RuntimeError(f"transform failed on {record.audit_id}")


class _FlakyAuditWriter(_CollectingAuditWriter):
    """Fails the first ``failures`` attempts outright, then behaves normally."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._remaining = failures

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        if self._remaining > 0:
            self._remaining -= 1
            self.attempted_ids.append(record.audit_id)
            raise ConnectionError("audit write failed")
        return await super().write(record)


class _AlwaysFailingAuditWriter(_CollectingAuditWriter):
    """Never persists anything."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempted_ids.append(record.audit_id)
        raise ConnectionError("audit write failed")


class _CrashAfterCommitWriter(_CollectingAuditWriter):
    """Persists the row and *then* raises -- the partial-success case."""

    def __init__(self) -> None:
        super().__init__()
        self._crashed = False

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        stored = await super().write(record)
        if not self._crashed:
            self._crashed = True
            raise ConnectionError("connection reset after commit")
        return stored


class _BlockingAuditWriter(_CollectingAuditWriter):
    """Parks inside the write until released, so shutdown finds work in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        await self.released.wait()
        return await super().write(record)


def _record(audit_id: str, *, tenant_id: str = "tenant-a") -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        status="completed",
    )


def _queue(writer: Any, **kwargs: Any) -> AuditDeliveryQueue:
    kwargs.setdefault("base_delay_seconds", 0)
    kwargs.setdefault("max_delay_seconds", 0)
    return AuditDeliveryQueue(writer, **kwargs)


async def test_submit_is_synchronous_so_no_producer_can_await_the_audit_writer() -> None:
    # R1: the structural half. A coroutine submit could be made to block on a
    # full queue; a plain method cannot.
    assert not inspect.iscoroutinefunction(AuditDeliveryQueue.submit)


async def test_a_saturated_queue_rejects_the_newest_event_instead_of_growing() -> None:
    writer = _BlockingAuditWriter()
    queue = _queue(writer, max_queue_size=2)

    accepted = [queue.submit(_record(f"audit-{index}")) for index in range(3)]

    assert accepted == [True, True, False]
    assert queue.pending == 2
    assert queue.counts().queued == 2
    assert queue.counts().rejected == 1
    writer.released.set()
    await queue.aclose(timeout=1.0)


async def test_a_rejected_event_is_never_handed_to_the_writer() -> None:
    writer = _CollectingAuditWriter()
    queue = _queue(writer, max_queue_size=1)

    assert queue.submit(_record("audit-kept")) is True
    assert queue.submit(_record("audit-rejected")) is False

    report = await queue.aclose(timeout=1.0)
    assert report.drained is True
    assert [stored.audit_id for stored in writer.records] == ["audit-kept"]


async def test_a_transient_failure_is_retried_and_persists_exactly_one_record() -> None:
    writer = _FlakyAuditWriter(failures=2)
    queue = _queue(writer, max_attempts=3)

    queue.submit(_record("audit-retried"))
    report = await queue.aclose(timeout=1.0)

    assert report.drained is True
    assert len(writer.records) == 1
    assert report.counts.retried == 2
    assert report.counts.delivered == 1
    assert report.counts.failed == 0


async def test_every_retry_reuses_the_audit_id_minted_at_submit_time() -> None:
    # R2's trap: a fresh uuid4 per attempt would write one event twice.
    writer = _FlakyAuditWriter(failures=2)
    queue = _queue(writer, max_attempts=3)

    queue.submit(_record("audit-stable"))
    await queue.aclose(timeout=1.0)

    assert writer.attempted_ids == ["audit-stable", "audit-stable", "audit-stable"]
    assert set(writer.attempted_ids) == {"audit-stable"}


async def test_a_retry_after_a_partially_succeeded_write_counts_as_delivered() -> None:
    # The writer commits, then the connection drops. The retry hits the
    # append-only duplicate check, which means "already durable", not "error".
    writer = _CrashAfterCommitWriter()
    queue = _queue(writer, max_attempts=3)

    queue.submit(_record("audit-committed"))
    report = await queue.aclose(timeout=1.0)

    assert len(writer.records) == 1
    assert writer.attempted_ids == ["audit-committed", "audit-committed"]
    assert report.counts.delivered == 1
    assert report.counts.retried == 1
    assert report.counts.failed == 0


async def test_an_event_that_exhausts_its_attempts_is_failed_and_not_delivered() -> None:
    writer = _AlwaysFailingAuditWriter()
    queue = _queue(writer, max_attempts=3)

    queue.submit(_record("audit-doomed"))
    report = await queue.aclose(timeout=1.0)

    assert writer.records == []
    assert len(writer.attempted_ids) == 3
    assert report.counts.failed == 1
    assert report.counts.delivered == 0
    assert report.counts.retried == 2


async def test_the_worker_keeps_delivering_after_an_event_fails_permanently() -> None:
    writer = _FlakyAuditWriter(failures=2)
    queue = _queue(writer, max_attempts=1)

    queue.submit(_record("audit-fails"))
    queue.submit(_record("audit-also-fails"))
    queue.submit(_record("audit-succeeds"))
    report = await queue.aclose(timeout=1.0)

    assert [stored.audit_id for stored in writer.records] == ["audit-succeeds"]
    assert report.counts.failed == 2
    assert report.counts.delivered == 1


async def test_the_counters_reach_the_injected_metrics_collector() -> None:
    # R7: queued, delivered, retried, rejected and failed are each observable.
    metrics = MetricsCollector()
    writer = _AlwaysFailingAuditWriter()
    queue = _queue(writer, max_queue_size=1, max_attempts=2, metrics=metrics)

    assert queue.submit(_record("audit-1")) is True
    assert queue.submit(_record("audit-2")) is False
    await queue.aclose(timeout=1.0)

    counters = metrics.snapshot()["counters"]
    assert counters["zeroth_audit_delivery_queued_total"] == 1
    assert counters["zeroth_audit_delivery_retried_total"] == 1
    assert counters["zeroth_audit_delivery_failed_total"] == 1
    assert counters['zeroth_audit_delivery_rejected_total{reason="queue_full"}'] == 1
    assert "zeroth_audit_delivery_delivered_total" not in counters
    assert metrics.snapshot()["gauges"][QUEUE_DEPTH_GAUGE] == 0.0


async def test_the_queue_depth_gauge_tracks_the_backlog() -> None:
    metrics = MetricsCollector()
    writer = _BlockingAuditWriter()
    queue = _queue(writer, max_queue_size=4, metrics=metrics)

    queue.submit(_record("audit-1"))
    queue.submit(_record("audit-2"))

    assert metrics.snapshot()["gauges"][QUEUE_DEPTH_GAUGE] == 2.0
    writer.released.set()
    await queue.aclose(timeout=1.0)
    assert metrics.snapshot()["gauges"][QUEUE_DEPTH_GAUGE] == 0.0


async def test_aclose_drains_in_flight_work_and_reports_nothing_undelivered() -> None:
    # R10: the graceful path.
    writer = _CollectingAuditWriter()
    queue = _queue(writer, max_queue_size=8)

    for index in range(5):
        queue.submit(_record(f"audit-{index}"))
    report = await queue.aclose(timeout=1.0)

    assert report.drained is True
    assert report.undelivered_audit_ids == ()
    assert report.counts.delivered == 5
    assert len(writer.records) == 5


async def test_aclose_reports_the_events_it_could_not_deliver_within_the_bound() -> None:
    # R10: the bounded path. One event is mid-write, two are still queued;
    # all three must be named rather than silently discarded.
    writer = _BlockingAuditWriter()
    queue = _queue(writer, max_queue_size=8)

    for index in range(3):
        queue.submit(_record(f"audit-{index}"))
    report = await queue.aclose(timeout=0.05)

    assert report.drained is False
    assert report.undelivered_audit_ids == ("audit-0", "audit-1", "audit-2")
    assert report.counts.abandoned == 3
    assert report.counts.delivered == 0
    assert writer.records == []


async def test_submit_after_aclose_is_rejected_rather_than_silently_dropped() -> None:
    writer = _CollectingAuditWriter()
    queue = _queue(writer)

    queue.submit(_record("audit-before"))
    await queue.aclose(timeout=1.0)

    assert queue.submit(_record("audit-after")) is False
    assert queue.counts().rejected == 1
    assert [stored.audit_id for stored in writer.records] == ["audit-before"]


async def test_aclose_is_idempotent() -> None:
    writer = _CollectingAuditWriter()
    queue = _queue(writer)

    queue.submit(_record("audit-1"))
    first = await queue.aclose(timeout=1.0)
    second = await queue.aclose(timeout=1.0)

    assert first.drained is True
    assert second.drained is True
    assert second.undelivered_audit_ids == ()
    assert second.counts.delivered == 1


async def test_backoff_stays_within_the_configured_ceiling() -> None:
    queue = AuditDeliveryQueue(
        _CollectingAuditWriter(),
        base_delay_seconds=1.0,
        max_delay_seconds=4.0,
    )

    worker = queue._loop_worker
    delays = [worker._backoff_delay(attempt) for attempt in range(1, 8) for _ in range(20)]

    assert all(0.0 <= delay <= 4.0 for delay in delays)
    assert len(set(delays)) > 1  # jittered, not a fixed schedule


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_queue_size": 0},
        {"max_queue_size": 1.0},
        {"max_attempts": 0},
        {"max_attempts": True},
        {"base_delay_seconds": -1.0},
        {"max_delay_seconds": "5"},
        {"base_delay_seconds": 10.0, "max_delay_seconds": 1.0},
        # A non-finite delay is not a bound: NaN compares false against every
        # comparison a later check could make, and infinity passes them all.
        {"base_delay_seconds": float("nan")},
        {"base_delay_seconds": float("inf")},
        {"base_delay_seconds": float("-inf")},
        {"max_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"max_delay_seconds": float("-inf")},
    ],
)
async def test_invalid_configuration_is_rejected_at_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AuditDeliveryQueue(_CollectingAuditWriter(), **kwargs)


async def test_an_event_without_an_audit_id_is_rejected_at_submit() -> None:
    queue = _queue(_CollectingAuditWriter())

    with pytest.raises(ValueError, match="audit_id"):
        queue.submit(_record(""))


async def test_an_event_without_a_tenant_is_rejected_at_submit() -> None:
    # R9: the worker has no ambient tenant, and a blank one would be persisted
    # against the reserved "default" tenant with its fallback retention TTL.
    queue = _queue(_CollectingAuditWriter())

    with pytest.raises(ValueError, match="tenant_id"):
        queue.submit(_record("audit-1", tenant_id=""))

    assert queue.pending == 0
    assert queue.counts().queued == 0


async def test_the_submitted_tenant_is_the_tenant_that_reaches_the_writer() -> None:
    # R9: capture classification rewrites the content channels; tenancy is not
    # one of them, so the record lands under the tenant the producer named.
    writer = _CollectingAuditWriter()
    queue = _queue(writer)

    queue.submit(_record("audit-1", tenant_id="tenant-b"))
    await queue.aclose(timeout=1.0)

    assert [record.tenant_id for record in writer.records] == ["tenant-b"]

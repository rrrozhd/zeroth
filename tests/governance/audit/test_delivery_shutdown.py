"""Graceful shutdown of the audit delivery stage: its bound, and what it reports.

``aclose`` is the only moment the stage gets to say what it lost, so these tests
pin R7 and R10 from the adversarial side rather than the happy one: the counters
must stay exact when two callers close at once, an event that exhausted its
retries must still be *named* rather than reduced to a number, and the deadline
must hold against a writer that ignores cancellation instead of only against one
that cooperates.

The final test runs the real :class:`AuditRepository` behind the queue, because
the bound is only meaningful if the writer the stage is actually pointed at
cannot outlive it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.repository import AuditRepository


class _AlwaysFailingWriter:
    """Never persists anything, so every event exhausts its attempts."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        raise ConnectionError("audit write failed")


class _BlockingWriter:
    """Parks inside the write until cancelled, so shutdown finds work in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FailThenBlockWriter:
    """Fails ``audit-failed`` outright, then parks on the next event."""

    def __init__(self) -> None:
        self.blocked = asyncio.Event()

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        if record.audit_id == "audit-failed":
            raise ConnectionError("audit write failed")
        self.blocked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _CancellationResistantWriter:
    """Swallows cancellation -- the contract violation the deadline must survive.

    A writer like this kept the previous ``aclose`` running for many times its
    configured bound, because only the drain was bounded and the cancellation
    that followed was awaited without one.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:  # noqa: PERF203 - the violation under test
                continue
        self.finished.set()
        return record


def _record(audit_id: str) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        status="completed",
    )


def _queue(writer: Any, **kwargs: Any) -> AuditDeliveryQueue:
    kwargs.setdefault("base_delay_seconds", 0)
    kwargs.setdefault("max_delay_seconds", 0)
    return AuditDeliveryQueue(writer, **kwargs)


async def test_an_event_that_exhausted_its_retries_is_named_in_the_close_report() -> None:
    # R10: the aggregate counter says one record was lost; only the id says
    # which one, and the id is what a recovery needs.
    queue = _queue(_AlwaysFailingWriter(), max_attempts=1)

    queue.submit(_record("audit-lost"))
    report = await queue.aclose(timeout=1.0)

    assert report.counts.failed == 1
    assert report.counts.abandoned == 0
    assert report.undelivered_audit_ids == ("audit-lost",)
    assert report.drained is False


async def test_failed_and_abandoned_stay_distinct_in_one_report() -> None:
    # "Ran out of attempts" and "was still in flight at the bound" are different
    # operator stories; the report carries both ids and both counters.
    writer = _FailThenBlockWriter()
    queue = _queue(writer, max_attempts=1, max_queue_size=8)

    queue.submit(_record("audit-failed"))
    queue.submit(_record("audit-blocked"))
    await asyncio.wait_for(writer.blocked.wait(), timeout=1.0)
    report = await queue.aclose(timeout=0.05)

    assert report.counts.failed == 1
    assert report.counts.abandoned == 1
    assert report.undelivered_audit_ids == ("audit-failed", "audit-blocked")
    assert report.drained is False


async def test_two_concurrent_closes_abandon_each_in_flight_event_exactly_once() -> None:
    # R7: two independent snapshots each abandoned and counted the same event,
    # so one lost record was reported as two.
    writer = _BlockingWriter()
    queue = _queue(writer)

    queue.submit(_record("audit-in-flight"))
    await asyncio.wait_for(writer.entered.wait(), timeout=1.0)
    first, second = await asyncio.gather(queue.aclose(timeout=0.05), queue.aclose(timeout=0.05))

    assert first.counts.abandoned == 1
    assert second.counts.abandoned == 1
    assert first.undelivered_audit_ids == ("audit-in-flight",)
    assert second is first


async def test_a_later_close_returns_the_report_the_first_close_produced() -> None:
    writer = _BlockingWriter()
    queue = _queue(writer)

    queue.submit(_record("audit-in-flight"))
    await asyncio.wait_for(writer.entered.wait(), timeout=1.0)
    first = await queue.aclose(timeout=0.05)
    second = await queue.aclose(timeout=0.05)

    assert second is first
    assert queue.counts().abandoned == 1


async def test_close_returns_within_its_bound_when_the_writer_ignores_cancellation() -> None:
    # R10: the deadline covers the drain *and* the cancellation. A write that
    # refuses to be cancelled is left running and reported, never waited on --
    # and it can no longer take the worker with it, because each attempt is its
    # own task and the worker waits on one only under a finite deadline.
    writer = _CancellationResistantWriter()
    queue = _queue(writer)
    loop = asyncio.get_running_loop()

    queue.submit(_record("audit-stuck"))
    await asyncio.wait_for(writer.entered.wait(), timeout=1.0)
    started = loop.time()
    report = await queue.aclose(timeout=0.05)
    elapsed = loop.time() - started

    assert elapsed < 1.0
    assert report.drained is False
    assert report.undelivered_audit_ids == ("audit-stuck",)

    # R7: the abandoned event stays abandoned. The write is still running at
    # this point; letting it run to completion produced ``abandoned=1`` *and*
    # ``delivered=1`` for one event, which made the two counters meaningless.
    writer.release.set()
    await asyncio.wait_for(writer.finished.wait(), timeout=1.0)
    await asyncio.sleep(0)
    counts = queue.counts()
    assert counts.abandoned == 1
    assert counts.delivered == 0


async def test_the_stage_owns_its_worker_task_and_retires_it_when_it_finishes() -> None:
    queue = _queue(_AlwaysFailingWriter(), max_attempts=1)

    queue.submit(_record("audit-1"))
    assert queue._worker in queue._tasks

    await queue.aclose(timeout=1.0)
    await asyncio.sleep(0)

    assert queue._tasks == set()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), -1.0, "5"])
async def test_a_shutdown_timeout_that_is_not_a_finite_bound_is_rejected(timeout: Any) -> None:
    # An unbounded or undefined drain is not a bounded shutdown.
    queue = _queue(_AlwaysFailingWriter())

    with pytest.raises(ValueError):
        await queue.aclose(timeout=timeout)


async def test_the_real_repository_write_cannot_outlive_the_shutdown_deadline(
    sqlite_db: Any,
) -> None:
    # The bound only means something against the writer this stage is pointed
    # at in production, so this one runs the real append-only repository.
    queue = _queue(AuditRepository.for_default_compatibility(sqlite_db), max_queue_size=64)
    loop = asyncio.get_running_loop()

    for index in range(40):
        assert queue.submit(_record(f"audit-{index}")) is True
    started = loop.time()
    report = await queue.aclose(timeout=0.01)
    elapsed = loop.time() - started

    assert elapsed < 2.0
    counts = report.counts
    assert counts.delivered + counts.failed + counts.abandoned == 40
    assert len(report.undelivered_audit_ids) == counts.failed + counts.abandoned

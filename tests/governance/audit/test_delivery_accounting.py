"""Every event the delivery stage accepts or refuses ends in exactly one counter.

R7 says the queued, retried, rejected and failed counts are accurate; R8 says a
failure is visible and never counted as a delivery. Three shapes broke that, and
each has a probe here.

*A refusal that moved nothing.* ``submit`` validated the record's identity and
tenancy before touching a counter, so the one event it refused outright
disappeared while ``/health`` and ``/v1/metrics`` both still said nothing had
been lost. A whitespace-only tenant was not refused at all -- ``"  "`` is truthy,
so the record was accepted and attributed to nobody.

*An attempt with no bound.* ``await writer.write(...)`` could hang forever,
taking the stage's only worker with it: no retry, no failure, no counter, and
nothing to see until the queue filled or shutdown began.

*Two counters for one event.* Shutdown marked an in-flight event abandoned and
then cancelled the worker; a write that completed anyway went on to increment
``delivered``, so one event was counted under two outcomes that are supposed to
be exclusive.
"""

from __future__ import annotations

import asyncio
import pytest

from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.delivery_state import (
    COUNTER_METRICS,
    IN_FLIGHT_AGE_GAUGE,
    DeliveryCounters,
    DeliveryFailure,
    DeliveryOutcome,
    DeliveryRejection,
    PendingAudit,
    TerminalState,
)
from zeroth.governance.audit.delivery_worker import DeliveryWorker
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.platform.observability.metrics import MetricsCollector


class _CollectingWriter:
    """Stores whatever it is handed, immediately."""

    def __init__(self) -> None:
        self.records: list[NodeAuditRecord] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Persist one record."""
        self.records.append(record)
        return record


class _HangingWriter:
    """Parks past its deadline and ignores cancellation -- the wedged-writer probe.

    ``release`` exists only so a test can let the abandoned attempts finish
    instead of leaving them parked on the loop; the stage itself never waits
    for it, which is the whole point.
    """

    def __init__(self, *, attempts_before_success: int = 99) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self._attempts_before_success = attempts_before_success

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Park until released, unless this attempt is the one told to succeed."""
        self.calls += 1
        self.entered.set()
        if self.calls > self._attempts_before_success:
            return record
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:  # noqa: PERF203 - the violation under test
                continue
        return record


class _LateCommittingWriter:
    """Ignores the deadline's cancellation and commits well after it passed.

    The probe for an attempt the stage released: it is never waited on, so the
    row it eventually writes appears with nobody on the stack to notice.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.written: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Park through every cancellation, then persist once released."""
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:  # noqa: PERF203 - the violation under test
                continue
        self.written.append(record.audit_id)
        return record


async def _settle_until(predicate, *, timeout: float = 1.0) -> None:
    """Yield to the loop until a predicate holds, failing at its own bound."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("the condition was never reached within its bound")
        await asyncio.sleep(0.001)


def _record(audit_id: str = "audit-1", *, tenant_id: str = "tenant-a") -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        status="completed",
    )


@pytest.mark.parametrize("tenant_id", ["", "   ", "\t\n"])
async def test_a_blank_tenant_is_refused_and_the_refusal_is_counted(tenant_id: str) -> None:
    # The reproduction: an empty tenant left queued, rejected and failed all at
    # zero, so the event vanished while every health surface read "ok". A
    # whitespace-only tenant was not even refused.
    queue = AuditDeliveryQueue(_CollectingWriter())

    with pytest.raises(ValueError, match="tenant_id"):
        queue.submit(_record(tenant_id=tenant_id))

    counts = queue.counts()
    assert counts.queued == 0
    assert counts.rejected == 1
    assert queue.pending == 0


async def test_an_invalid_record_rejection_carries_its_own_reason_on_the_metric() -> None:
    metrics = MetricsCollector()
    queue = AuditDeliveryQueue(_CollectingWriter(), metrics=metrics)

    with pytest.raises(ValueError):
        queue.submit(_record(tenant_id=" "))

    rendered = metrics.render_prometheus_text()
    assert COUNTER_METRICS["rejected"] in rendered
    assert DeliveryRejection.INVALID_RECORD.value in rendered


async def test_a_pre_submit_failure_reaches_the_same_counters_health_reads() -> None:
    # A producer can lose an event before ``submit`` ever sees a record; that
    # loss used to reach nothing but a log line on the producer's side.
    queue = AuditDeliveryQueue(_CollectingWriter())

    queue.reject("audit-unprojectable", DeliveryRejection.PROJECTION_FAILED)

    assert queue.counts().rejected == 1


async def test_a_hung_write_is_bounded_retried_and_finally_counted_as_a_failure() -> None:
    # The reproduction: one hung write owned the only worker forever -- no
    # retry, no failure, no counter movement at all.
    writer = _HangingWriter()
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=2,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
    )

    assert queue.submit(_record()) is True
    report = await queue.aclose(timeout=1.0)

    writer.release.set()
    assert writer.calls == 2
    assert report.counts.retried == 1
    assert report.counts.failed == 1
    assert report.counts.delivered == 0
    assert report.undelivered_audit_ids == ("audit-1",)


async def test_a_write_that_overruns_its_deadline_is_retried_under_the_same_identity() -> None:
    # The retry is the same ``audit_id``, so a hung attempt that lands later is
    # absorbed by the append-only duplicate check rather than persisted twice.
    writer = _HangingWriter(attempts_before_success=1)
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=3,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
    )

    assert queue.submit(_record()) is True
    report = await queue.aclose(timeout=1.0)

    writer.release.set()
    assert report.counts.delivered == 1
    assert report.counts.failed == 0
    assert report.drained is True


async def test_the_age_of_a_write_that_overran_its_deadline_is_published() -> None:
    # A wedged writer moves no counter until it finally fails; the age is what
    # says "stuck", not "idle", while it is still stuck.
    metrics = MetricsCollector()
    writer = _HangingWriter()
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
        metrics=metrics,
    )

    assert queue.submit(_record()) is True
    await queue.aclose(timeout=1.0)
    writer.release.set()

    assert IN_FLIGHT_AGE_GAUGE in metrics.render_prometheus_text()


async def test_the_published_in_flight_age_is_cleared_once_the_event_leaves_the_worker() -> None:
    # The reproduction: the gauge was set at a timeout and never reset, so an
    # idle worker went on presenting a resolved wedge's duration as the age of a
    # write that is currently outstanding -- and no write was outstanding at all.
    metrics = MetricsCollector()
    writer = _HangingWriter()
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
        metrics=metrics,
    )

    assert queue.submit(_record()) is True
    await queue.aclose(timeout=1.0)
    writer.release.set()

    assert metrics.snapshot()["gauges"][IN_FLIGHT_AGE_GAUGE] == 0.0
    assert queue.in_flight_seconds is None


async def test_a_write_that_commits_after_its_deadline_reconciles_the_event_it_lost() -> None:
    # The reproduction: the final attempt overran its deadline and was released,
    # the event was counted as failed, and the write then landed -- leaving a
    # durable row an operator was told to go and recover, and a report that said
    # ``failed=1, reconciled=0`` about a record that exists.
    writer = _LateCommittingWriter()
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
    )

    assert queue.submit(_record()) is True
    report = await queue.aclose(timeout=1.0)
    assert report.counts.failed == 1
    assert report.counts.reconciled == 0

    writer.release.set()
    await _settle_until(lambda: queue.counts().reconciled == 1)

    counts = queue.counts()
    assert writer.written == ["audit-1"]
    assert counts.reconciled == 1
    assert counts.failed == 1
    assert counts.delivered == 0


async def test_two_released_attempts_that_both_land_reconcile_the_event_once() -> None:
    # R7: reconciliation is a claim, not an increment. Every attempt of one
    # ``audit_id`` can come back, and one durable record is one reconciliation.
    writer = _LateCommittingWriter()
    queue = AuditDeliveryQueue(
        writer,
        max_attempts=2,
        base_delay_seconds=0,
        max_delay_seconds=0,
        write_timeout_seconds=0.01,
    )

    assert queue.submit(_record()) is True
    report = await queue.aclose(timeout=1.0)
    assert report.counts.failed == 1

    writer.release.set()
    await _settle_until(lambda: len(writer.written) == 2)
    await asyncio.sleep(0)

    counts = queue.counts()
    assert counts.reconciled == 1
    assert counts.delivered == 0


async def test_a_wedged_write_reports_how_long_it_has_been_outstanding() -> None:
    writer = _HangingWriter()
    queue = AuditDeliveryQueue(writer, write_timeout_seconds=5.0)

    assert queue.submit(_record()) is True
    await asyncio.wait_for(writer.entered.wait(), timeout=1.0)

    age = queue.in_flight_seconds
    assert age is not None
    assert age >= 0.0
    await queue.aclose(timeout=0.05)
    writer.release.set()


def test_one_event_can_only_claim_one_terminal_outcome() -> None:
    state = TerminalState()

    assert state.claim(DeliveryOutcome.ABANDONED) is True
    assert state.claim(DeliveryOutcome.DELIVERED) is False
    assert state.outcome is DeliveryOutcome.ABANDONED


async def test_a_commit_after_abandonment_is_reconciled_rather_than_delivered() -> None:
    # The reproduction ended with ``abandoned=1, delivered=1`` for one event.
    # The late commit is real -- the row is durable -- so it is recorded, but
    # under its own name, never as a second reading of the same event.
    counters = DeliveryCounters(MetricsCollector(), max_retained_ids=8)
    worker = DeliveryWorker(
        writer=_CollectingWriter(),
        counters=counters,
        queue=asyncio.Queue(),
        max_attempts=1,
        base_delay=0.0,
        max_delay=0.0,
    )
    item = PendingAudit(audit_id="audit-1", record=_record())

    assert item.terminal.claim(DeliveryOutcome.ABANDONED) is True
    counters.increment("abandoned")
    worker.commit(item)

    counts = counters.snapshot()
    assert counts.abandoned == 1
    assert counts.delivered == 0
    assert counts.reconciled == 1


async def test_a_failure_after_abandonment_is_not_counted_a_second_time() -> None:
    counters = DeliveryCounters(MetricsCollector(), max_retained_ids=8)
    worker = DeliveryWorker(
        writer=_CollectingWriter(),
        counters=counters,
        queue=asyncio.Queue(),
        max_attempts=1,
        base_delay=0.0,
        max_delay=0.0,
    )
    item = PendingAudit(audit_id="audit-1", record=_record())

    assert item.terminal.claim(DeliveryOutcome.ABANDONED) is True
    counters.increment("abandoned")
    worker.fail(item, DeliveryFailure.WRITE_FAILED)

    counts = counters.snapshot()
    assert counts.abandoned == 1
    assert counts.failed == 0


async def test_a_write_timeout_that_is_not_a_positive_bound_is_rejected() -> None:
    for invalid in (0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            AuditDeliveryQueue(_CollectingWriter(), write_timeout_seconds=invalid)

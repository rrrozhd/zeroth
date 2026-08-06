"""What ``AuditGatewayEventSink.emit`` does when the hand-off itself misbehaves.

``emit`` runs inside a streaming response's ``finally``, and its whole contract
is that it never raises there: every way an event fails to become durable is a
counted rejection, because the alternative is the proxy's generic handler
logging one full traceback per event on the response-completion path.

The guard used to be narrow enough to be decorative. It caught ``ValueError``
from ``submit`` -- the one exception the *production* stage raises -- while the
submitter is a :class:`~zeroth.governance.langgraph_gateway.events.AuditRecordSubmitter`
that any implementation may satisfy. A supported injected submitter raising
``RuntimeError`` escaped straight through, carrying its own message into a
traceback; so did a ``reject`` that failed while accounting for the first
failure. Both are probed here with an exception message holding a secret,
because that message is what a traceback would publish.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from zeroth.contracts.langgraph_gateway.models import (
    GatewayCorrelation,
    GatewayEvent,
    GatewayEventStatus,
    GovernanceLevel,
    RouteDisposition,
)
from zeroth.governance.audit.delivery import DeliveryRejection
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.langgraph_gateway.events import AuditGatewayEventSink

SUBMITTER_SECRET = "sk-proj-REFUSAL-MESSAGE-PROBE"


class _RecordingRepository:
    """The writer argument the sink requires and these tests never reach."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Persist nothing: the hand-off under test never gets this far."""
        raise AssertionError("the delivery stage is injected; this is unreachable")


class _RaisingSubmitter:
    """A supported submitter whose hand-off raises instead of refusing."""

    def __init__(self, *, reject_raises: BaseException | None = None) -> None:
        self.rejections: list[tuple[str, DeliveryRejection]] = []
        self._reject_raises = reject_raises

    def submit(self, record: NodeAuditRecord) -> bool:
        """Fail the hand-off the way a foreign implementation can."""
        raise RuntimeError(f"refusal holding {SUBMITTER_SECRET}")

    def reject(self, audit_id: str, reason: DeliveryRejection) -> None:
        """Account for one lost event, or fail at that too."""
        self.rejections.append((audit_id, reason))
        if self._reject_raises is not None:
            raise self._reject_raises


class _CancellingSubmitter:
    """A submitter cancelled mid-hand-off, as a shutting-down loop would."""

    def __init__(self) -> None:
        self.rejections: list[tuple[str, DeliveryRejection]] = []

    def submit(self, record: NodeAuditRecord) -> bool:
        """Raise the one exception that must never be swallowed."""
        raise asyncio.CancelledError

    def reject(self, audit_id: str, reason: DeliveryRejection) -> None:
        """Record an accounting call that must never happen on a cancellation."""
        self.rejections.append((audit_id, reason))


def _event(*, tenant_id: str = "tenant-a") -> GatewayEvent:
    """Build one terminal gateway event the sink can project."""
    started = datetime(2026, 7, 25, 12, tzinfo=UTC)
    return GatewayEvent(
        correlation=GatewayCorrelation(
            correlation_id="corr-1",
            deployment_ref="deployment-a",
            tenant_id=tenant_id,
            principal_id="user-7",
            assistant_id="assistant-2",
            thread_id="thread-4",
            run_id="run-7",
        ),
        operation="runs.stream",
        disposition=RouteDisposition.GOVERNED,
        governance_level=GovernanceLevel.ADMISSION,
        status=GatewayEventStatus.SUCCESS,
        started_at=started,
        completed_at=started,
    )


def _sink(delivery: object) -> AuditGatewayEventSink:
    """Build the sink over an injected delivery stage."""
    return AuditGatewayEventSink(
        _RecordingRepository(), actor_for=lambda _event: None, delivery=delivery
    )


def _messages(caplog) -> str:
    """Join every captured log message, so an assertion can read the whole stream."""
    return " ".join(record.getMessage() for record in caplog.records)


async def test_a_hand_off_that_raises_is_counted_rather_than_thrown_at_the_proxy(caplog) -> None:
    # The reproduction: only ``ValueError`` was caught, so a submitter raising
    # ``RuntimeError`` reached ``GatewayProxy``'s ``logger.exception`` -- one
    # traceback per event, carrying the submitter's own message.
    delivery = _RaisingSubmitter()
    sink = _sink(delivery)

    with caplog.at_level(logging.DEBUG):
        await sink.emit(_event())

    [(audit_id, reason)] = delivery.rejections
    assert audit_id.startswith("langgraph.gateway:")
    assert reason is DeliveryRejection.SUBMIT_FAILED
    assert SUBMITTER_SECRET not in _messages(caplog)


async def test_an_accounting_call_that_also_fails_still_never_reaches_the_producer(
    caplog,
) -> None:
    # The last resort: the counter itself is the injected submitter's, so it can
    # fail the same way the hand-off did. All that may be said about it is a
    # fixed code and an exception type.
    delivery = _RaisingSubmitter(reject_raises=RuntimeError(f"reject holding {SUBMITTER_SECRET}"))
    sink = _sink(delivery)

    with caplog.at_level(logging.ERROR, logger="zeroth.governance.langgraph_gateway.events"):
        await sink.emit(_event())

    emitted = _messages(caplog)
    assert "audit_accounting_failed" in emitted
    assert "RuntimeError" in emitted
    assert SUBMITTER_SECRET not in emitted


async def test_a_projection_failure_survives_an_accounting_call_that_raises(caplog) -> None:
    # The same guard on the other producer path: an unprojectable event whose
    # rejection raises must still cost one counted event, not a traceback.
    delivery = _RaisingSubmitter(reject_raises=RuntimeError(f"reject holding {SUBMITTER_SECRET}"))

    def _exploding_actor(_event: GatewayEvent) -> None:
        raise RuntimeError("actor resolution failed")

    sink = AuditGatewayEventSink(
        _RecordingRepository(), actor_for=_exploding_actor, delivery=delivery
    )

    with caplog.at_level(logging.ERROR, logger="zeroth.governance.langgraph_gateway.events"):
        await sink.emit(_event())

    [(_audit_id, reason)] = delivery.rejections
    assert reason is DeliveryRejection.PROJECTION_FAILED
    assert SUBMITTER_SECRET not in _messages(caplog)


async def test_a_cancelled_hand_off_is_re_raised_rather_than_counted_as_a_refusal() -> None:
    # A cancelled response is not a lost audit event, and a broad guard that
    # swallowed the cancellation would leave the loop's shutdown unaware of it.
    delivery = _CancellingSubmitter()
    sink = _sink(delivery)

    try:
        await sink.emit(_event())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("the cancellation was swallowed")

    assert delivery.rejections == []

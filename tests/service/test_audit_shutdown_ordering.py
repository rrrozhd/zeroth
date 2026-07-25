"""What the service lifespan does *first* when serving stops, and what it consumes.

R10 properties that the wiring tests in ``test_audit_delivery_wiring.py`` cannot
state, because they are about the teardown's *order* and its edges rather than
its outcome.

* The bounded audit drain runs before the runtime's own post-yield teardown,
  not after it. That teardown is a sequence of unbounded awaits -- a run
  worker's graceful shutdown, the ARQ consumer and pool, the webhook client,
  the secret provider -- and the drain queued behind all of them was bounded on
  paper only: one hung predecessor postponed it for as long as the hang lasted.
* Moving the stop and the drain *inside* the runtime lifespan is what bounded
  them, and it is also what put both behind a startup that succeeds. A startup
  that fails never reaches the body, so an inner-only shutdown never ran at
  all: the transport the bootstrap had already built was left open and the
  accepted audit backlog was left unreported. The outer guard runs them for
  exactly that case -- and, because a normal shutdown already ran them, exactly
  once either way.
* A shutdown task abandoned at its bound still has its result consumed. An
  unretrieved task exception is reported by asyncio itself, which prints the
  exception's full message and traceback -- an injected transport's message, on
  the one path this process keeps free of foreign text.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI

from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.service.bootstrap import lifecycle
from zeroth.service.bootstrap.lifecycle import service_lifespan

TRANSPORT_SECRET = "sk-proj-TRANSPORT-CLOSE-PROBE"


class _CollectingAuditWriter:
    """Persists whatever the drain hands it, immediately."""

    def __init__(self) -> None:
        self.written: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Persist one record."""
        self.written.append(record.audit_id)
        return record


class _HangingRunWorker:
    """A run worker whose graceful shutdown parks -- the hung predecessor.

    Stands where the real worker stands in the runtime lifespan: started before
    the yield, polled by a task, and awaited without any bound after it.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        """Accept the startup call the runtime lifespan makes."""

    async def poll_loop(self) -> None:
        """Park until the lifespan cancels this task."""
        await asyncio.Event().wait()

    async def graceful_shutdown(self) -> None:
        """Hang for as long as the test allows, on the loop the drain needs."""
        self.entered.set()
        await self.release.wait()


class _RefusingRunWorker:
    """A run worker whose start fails, so the runtime lifespan never yields.

    Stands where the real worker stands: it is the first thing the runtime
    lifespan awaits, well before the ``yield``, so a refusal here is a startup
    that never reaches the body at all.
    """

    async def start(self) -> None:
        """Refuse to start, the way a worker with no reachable store does."""
        raise RuntimeError("run worker refused to start")

    async def poll_loop(self) -> None:
        """Never reached: the refusal above precedes this task's creation."""
        raise AssertionError("a refused startup must not have started polling")

    async def graceful_shutdown(self) -> None:
        """Never reached: post-yield teardown does not run for a failed start."""
        raise AssertionError("a refused startup must not reach the teardown")


class _CountingTransport:
    """Closes cleanly and counts how many times it was asked to."""

    def __init__(self) -> None:
        self.closes = 0

    async def aclose(self) -> None:
        """Record one close."""
        self.closes += 1


class _RefusingTransport:
    """Fails its close immediately, holding a secret in the message."""

    def __init__(self) -> None:
        self.closes = 0

    async def aclose(self) -> None:
        """Count the call, then fail the way a broken connection pool does."""
        self.closes += 1
        raise ConnectionError(f"transport close failed holding {TRANSPORT_SECRET}")


class _LateFailingTransport:
    """Ignores the close bound's cancellation, then fails holding a secret."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def aclose(self) -> None:
        """Park past the bound, then raise the way a broken pool does."""
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:  # noqa: PERF203 - the violation under test
                continue
        raise ConnectionError(f"transport close failed holding {TRANSPORT_SECRET}")


def _record(audit_id: str) -> NodeAuditRecord:
    """Build the minimal valid audit record the delivery stage accepts."""
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        status="completed",
    )


def _lifespan_app(**bootstrap_fields: object) -> FastAPI:
    """Build a bare app whose bootstrap holds only what the lifespan reads."""
    app = FastAPI()
    app.state.bootstrap = SimpleNamespace(**bootstrap_fields)
    return app


async def _settle_until(predicate, *, timeout: float = 2.0) -> None:
    """Yield to the loop until a predicate holds, failing at its own bound."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("the condition was never reached within its bound")
        await asyncio.sleep(0.001)


async def test_a_hung_runtime_teardown_cannot_postpone_the_bounded_audit_drain() -> None:
    # The reproduction: the drain sat outside the runtime lifespan, so it ran
    # only once every unbounded post-yield await had returned. A worker whose
    # graceful shutdown hangs held the accepted audit backlog hostage for the
    # whole hang -- the same lost evidence R10 forbids, reached more slowly.
    writer = _CollectingAuditWriter()
    queue = AuditDeliveryQueue(writer, base_delay_seconds=0, max_delay_seconds=0)
    worker = _HangingRunWorker()
    app = _lifespan_app(worker=worker, audit_delivery_queue=queue)

    manager = service_lifespan(app)
    await manager.__aenter__()
    assert queue.submit(_record("audit-before-a-hung-teardown")) is True
    teardown = asyncio.create_task(manager.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(worker.entered.wait(), timeout=2.0)
        # The hang is entered and still parked, so everything asserted here
        # happened strictly before it.
        assert writer.written == ["audit-before-a-hung-teardown"]
        assert queue.counts().delivered == 1
    finally:
        worker.release.set()
        await asyncio.wait_for(teardown, timeout=2.0)


async def test_a_runtime_lifespan_that_fails_to_start_still_closes_and_drains() -> None:
    # The regression the reorder introduced: with the stop and the drain moved
    # inside the runtime lifespan, a startup that never reaches the body never
    # ran either. The bootstrap builds the transport and the delivery stage
    # before the lifespan starts, so both outlive a failed start -- the
    # transport left open, and every event already accepted left unpersisted
    # and unreported, which is the loss R10 exists to prevent.
    writer = _CollectingAuditWriter()
    queue = AuditDeliveryQueue(writer, base_delay_seconds=0, max_delay_seconds=0)
    transport = _CountingTransport()
    app = _lifespan_app(
        worker=_RefusingRunWorker(),
        langgraph_gateway_transport=transport,
        audit_delivery_queue=queue,
    )
    assert queue.submit(_record("audit-accepted-before-a-failed-startup")) is True

    try:
        await service_lifespan(app).__aenter__()
    except RuntimeError as exc:
        assert "run worker refused to start" in str(exc)
    else:
        raise AssertionError("the startup failure was swallowed")

    assert transport.closes == 1
    assert writer.written == ["audit-accepted-before-a-failed-startup"]
    assert queue.counts().delivered == 1
    # Nothing is left running behind a lifespan that never yielded and will
    # never be exited: the stage is closed, so a later submit is refused and
    # counted rather than queued onto a worker no shutdown will ever reach.
    assert queue.submit(_record("audit-after-a-failed-startup")) is False
    assert queue.counts().rejected == 1


async def test_a_transport_that_fails_its_close_during_a_failed_startup_never_masks_it(
    caplog,
) -> None:
    # Two failures at once. The startup error is the one an operator needs, so
    # it is what propagates; the close's own message is an injected client's,
    # so it reaches the log as a fixed code and an exception type and never as
    # text -- and re-raising it would have replaced the startup error outright.
    transport = _RefusingTransport()
    app = _lifespan_app(
        worker=_RefusingRunWorker(),
        langgraph_gateway_transport=transport,
        audit_delivery_queue=None,
    )

    with caplog.at_level(logging.ERROR, logger="zeroth.service.bootstrap.lifecycle"):
        try:
            await service_lifespan(app).__aenter__()
        except RuntimeError as exc:
            assert "run worker refused to start" in str(exc)
        else:
            raise AssertionError("the transport failure masked the startup failure")

    emitted = _messages(caplog)
    assert transport.closes == 1
    assert "startup_transport_close_failed" in emitted
    assert "ConnectionError" in emitted
    assert TRANSPORT_SECRET not in emitted


async def test_a_normal_shutdown_closes_the_transport_once_and_not_again_at_the_guard() -> None:
    # The other half of the same guard. It exists for a startup that never
    # reached the body, so a shutdown that did reach it must not close a
    # transport the inner stop already closed: ``aclose`` belongs to an injected
    # client and nothing in its contract requires a second call to be a no-op.
    writer = _CollectingAuditWriter()
    queue = AuditDeliveryQueue(writer, base_delay_seconds=0, max_delay_seconds=0)
    transport = _CountingTransport()
    app = _lifespan_app(langgraph_gateway_transport=transport, audit_delivery_queue=queue)

    async with service_lifespan(app):
        assert queue.submit(_record("audit-on-a-normal-shutdown")) is True

    assert transport.closes == 1
    assert writer.written == ["audit-on-a-normal-shutdown"]
    assert queue.counts().delivered == 1


async def test_an_abandoned_transport_close_that_fails_later_is_logged_by_code_and_type(
    caplog,
) -> None:
    # The reproduction: the overrun close was cancelled and discarded, and its
    # eventual exception reached asyncio's default handler -- "Task exception was
    # never retrieved" plus the message the transport was holding.
    transport = _LateFailingTransport()
    app = _lifespan_app(langgraph_gateway_transport=transport, audit_delivery_queue=None)

    with caplog.at_level(logging.ERROR, logger="zeroth.service.bootstrap.lifecycle"):
        with mock.patch.object(lifecycle, "TRANSPORT_CLOSE_TIMEOUT_SECONDS", 0.01):
            async with service_lifespan(app):
                pass
        assert transport.entered.is_set()
        transport.release.set()
        await _settle_until(lambda: "abandoned_task_failed" in _messages(caplog))

    emitted = _messages(caplog)
    assert TRANSPORT_SECRET not in emitted
    assert "ConnectionError" in emitted


def _messages(caplog) -> str:
    """Join every captured log message, so an assertion can read the whole stream."""
    return " ".join(record.getMessage() for record in caplog.records)

"""What the service lifespan does *first* when serving stops, and what it consumes.

Two R10 properties that the wiring tests in ``test_audit_delivery_wiring.py``
cannot state, because both are about the teardown's *order* rather than its
outcome.

* The bounded audit drain runs before the runtime's own post-yield teardown,
  not after it. That teardown is a sequence of unbounded awaits -- a run
  worker's graceful shutdown, the ARQ consumer and pool, the webhook client,
  the secret provider -- and the drain queued behind all of them was bounded on
  paper only: one hung predecessor postponed it for as long as the hang lasted.
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

"""Application-level wiring of the bounded audit-delivery stage.

The stage's own behaviour is characterized in ``tests/governance/audit``. What
those tests cannot establish is whether any of it is *reachable*: a queue whose
counters land on a private collector nobody scrapes satisfies every component
test while being invisible in production, and a queue nobody drains loses its
backlog on every deploy. These tests pin the three application facts.

* **R7** -- the counters reach the same ``MetricsCollector`` the metrics
  endpoint renders, so ``queued``/``retried``/``rejected``/``failed`` are
  actually scrapeable.
* **R8** -- an event the stage lost shows up on the metrics endpoint and on
  both health surfaces, and is never rendered as a delivery.
* **R10** -- the service lifespan drains the queue, strictly after the gateway
  transport has stopped submitting into it and while the audit repository the
  worker writes through is still open.

Every queue here is driven from the test's own event loop and closed there, so
nothing is left owned by the ``TestClient`` portal loop.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.service.helpers import admin_headers, agent_graph, deploy_service
from zeroth.core.langgraph_gateway.models import CompatibilityResult, CompatibilityStatus
from zeroth.core.service.bootstrap import bootstrap_app
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.service.api.health import (
    DependencyStatus,
    audit_delivery_health,
    determine_readiness_status,
)
from zeroth.service.bootstrap.lifecycle import service_lifespan


class _AlwaysFailingWriter:
    """A durable write that never succeeds, so every event exhausts its attempts."""

    def __init__(self) -> None:
        self.attempted_ids: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Refuse the write, recording only that it was attempted."""
        self.attempted_ids.append(record.audit_id)
        raise ConnectionError("audit write failed")


class _GatedWriter:
    """Parks inside the write until a gate opens, then records the order it ran in."""

    def __init__(self, gate: asyncio.Event, order: list[str]) -> None:
        self.gate = gate
        self.order = order
        self.written: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Wait for the gate, then persist -- proving the write ran after it opened."""
        await self.gate.wait()
        self.order.append("audit_write")
        self.written.append(record.audit_id)
        return record


class _GateOpeningTransport:
    """A gateway transport whose shutdown is what releases the audit writer."""

    def __init__(self, gate: asyncio.Event, order: list[str]) -> None:
        self.gate = gate
        self.order = order

    async def aclose(self) -> None:
        """Stop the transport and let the parked audit write proceed."""
        self.order.append("gateway_transport")
        self.gate.set()


class _RecordingSecretProvider:
    """Stands in for the shared secret provider the runtime lifespan closes."""

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def aclose(self) -> None:
        """Record that the runtime teardown ran before the gateway and the drain."""
        self.order.append("secret_provider")


class _FakeGatewayTransport:
    """The transport the gateway bootstrap builds, without any upstream client."""

    def __init__(self, _settings: object, _secret_provider: object) -> None:
        self.client = SimpleNamespace(base_url=None)

    async def aclose(self) -> None:
        """Nothing to close: this transport never opened a connection."""


class _FakeCompatibilityDetector:
    """Returns the one bounded probe result without touching the network."""

    def __init__(self, _client: object, **_kwargs: Any) -> None:
        pass

    async def detect(self) -> CompatibilityResult:
        """Report a supported upstream so the gateway bootstrap continues."""
        return CompatibilityResult(
            tested_langgraph_versions=("1.2.9",),
            tested_agent_server_versions=("0.11.1",),
            detected_agent_server_version="0.11.1",
            status=CompatibilityStatus.SUPPORTED,
        )


class _CapturingProxy:
    """Captures the kwargs the factory hands the real proxy, chiefly the sink."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


class _InertHandler:
    """Accepts whatever the factory passes and does nothing with it."""

    def __init__(self, **_kwargs: Any) -> None:
        pass


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


async def _app_with_delivery(
    sqlite_db: Any, *, deployment_ref: str, writer: Any, **queue_kwargs: Any
) -> tuple[FastAPI, Any]:
    """Bootstrap a service app whose delivery stage publishes onto its collector."""
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=f"graph-{deployment_ref}"),
        deployment_ref=deployment_ref,
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    queue_kwargs.setdefault("base_delay_seconds", 0)
    queue_kwargs.setdefault("max_delay_seconds", 0)
    service.audit_delivery_queue = AuditDeliveryQueue(
        writer, metrics=service.metrics_collector, **queue_kwargs
    )
    return app, service


async def _gateway_bootstrap(sqlite_db: Any, monkeypatch: Any, graph_id: str) -> Any:
    """Bootstrap the service with the gateway enabled and every upstream faked."""
    from zeroth.core.config.settings import LangGraphGatewaySettings, get_settings
    from zeroth.core.signing import EnvHmacSigner
    from zeroth.service.bootstrap.factory import bootstrap_service

    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id=graph_id), deployment_ref=graph_id
    )
    gateway_settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server.test",
        upstream_audience="agent-server:test",
        deployment_ref=service.deployment.deployment_ref,
    )
    settings = get_settings().model_copy(update={"langgraph_gateway": gateway_settings})
    signer = EnvHmacSigner(key_id="test", keys={"test": b"gateway-signing-key"})

    async def fake_build_signer(_settings: object, _secret_provider: object) -> object:
        return signer

    factory = "zeroth.service.bootstrap.factory"
    monkeypatch.setattr(f"{factory}.get_settings", lambda: settings)
    monkeypatch.setattr(f"{factory}.build_signing_provider_async", fake_build_signer)
    monkeypatch.setattr(f"{factory}.HTTPGatewayTransport", _FakeGatewayTransport)
    monkeypatch.setattr(f"{factory}.CompatibilityDetector", _FakeCompatibilityDetector)
    monkeypatch.setattr(f"{factory}.CapabilityReporter", _InertHandler)
    monkeypatch.setattr(f"{factory}.GatewayProxy", _CapturingProxy)
    monkeypatch.setattr(f"{factory}.WebSocketGatewayHandler", _InertHandler)
    return await bootstrap_service(sqlite_db, deployment_ref=service.deployment.deployment_ref)


async def test_the_gateway_sink_delivers_into_the_queue_the_application_registry_scrapes(
    sqlite_db, monkeypatch
) -> None:
    """R7: one queue, shared by the sink, the bootstrap and the app's collector.

    The sink's private fallback builds its own ``MetricsCollector``; every
    counter published onto it is unreachable from ``/v1/metrics``. Identity is
    what makes the counters scrapeable, so identity is what is asserted.
    """
    service = await _gateway_bootstrap(sqlite_db, monkeypatch, "audit-delivery-wiring")

    delivery = service.audit_delivery_queue
    assert type(delivery) is AuditDeliveryQueue
    assert _CapturingProxy.last_kwargs["event_sink"].delivery is delivery

    await delivery.aclose(timeout=1.0)
    assert delivery.submit(_record("audit-after-close")) is False
    rendered = service.metrics_collector.render_prometheus_text()
    assert 'zeroth_audit_delivery_rejected_total{reason="closed"} 1' in rendered


async def test_a_failed_delivery_reaches_the_metrics_endpoint_and_is_never_a_delivery(
    sqlite_db,
) -> None:
    """R7 + R8: exhaustion is scrapeable, and ``delivered`` never moves for it."""
    writer = _AlwaysFailingWriter()
    app, service = await _app_with_delivery(
        sqlite_db,
        deployment_ref="audit-delivery-failed",
        writer=writer,
        max_attempts=2,
    )
    assert service.audit_delivery_queue.submit(_record("audit-failed")) is True
    report = await service.audit_delivery_queue.aclose(timeout=2.0)
    assert report.undelivered_audit_ids == ("audit-failed",)

    with TestClient(app) as client:
        rendered = client.get("/v1/metrics", headers=admin_headers()).text

    assert "zeroth_audit_delivery_failed_total 1" in rendered
    assert "zeroth_audit_delivery_retried_total 1" in rendered
    assert "zeroth_audit_delivery_delivered_total" not in rendered
    assert writer.attempted_ids == ["audit-failed", "audit-failed"]


async def test_a_saturated_delivery_stage_reports_the_refusal_rather_than_a_delivery(
    sqlite_db,
) -> None:
    """R8: the queue is finite, and the event it refused is counted as refused."""
    gate = asyncio.Event()
    writer = _GatedWriter(gate, [])
    app, service = await _app_with_delivery(
        sqlite_db,
        deployment_ref="audit-delivery-saturated",
        writer=writer,
        max_queue_size=1,
    )
    delivery = service.audit_delivery_queue
    assert delivery.submit(_record("audit-in-flight")) is True
    await asyncio.sleep(0)
    assert delivery.submit(_record("audit-queued")) is True
    assert delivery.submit(_record("audit-refused")) is False
    # Never released: the two accepted events are abandoned at the bound, which
    # is a third outcome and still not a delivery.
    report = await delivery.aclose(timeout=0.2)
    assert report.drained is False
    assert writer.written == []

    with TestClient(app) as client:
        rendered = client.get("/v1/metrics", headers=admin_headers()).text

    assert 'zeroth_audit_delivery_rejected_total{reason="queue_full"} 1' in rendered
    assert "zeroth_audit_delivery_abandoned_total 2" in rendered
    assert "zeroth_audit_delivery_delivered_total" not in rendered
    assert gate.is_set() is False


async def test_health_reports_the_backlog_and_the_loss_of_the_delivery_stage(
    sqlite_db,
) -> None:
    """R8: the same loss is visible on ``/health`` without a metrics scrape."""
    app, service = await _app_with_delivery(
        sqlite_db,
        deployment_ref="audit-delivery-health",
        writer=_AlwaysFailingWriter(),
        max_attempts=1,
    )
    assert service.audit_delivery_queue.submit(_record("audit-health")) is True
    await service.audit_delivery_queue.aclose(timeout=2.0)

    with TestClient(app) as client:
        payload = client.get("/health").json()["audit_delivery"]

    assert payload["failed"] == 1
    assert payload["delivered"] == 0
    assert payload["queue_depth"] == 0
    assert payload["losing_events"] is True


async def test_readiness_names_the_loss_as_a_failing_dependency(sqlite_db) -> None:
    """R8: the probe an orchestrator reads carries the loss and its counts."""
    app, service = await _app_with_delivery(
        sqlite_db,
        deployment_ref="audit-delivery-readiness",
        writer=_AlwaysFailingWriter(),
        max_attempts=1,
    )
    assert service.audit_delivery_queue.submit(_record("audit-readiness")) is True
    await service.audit_delivery_queue.aclose(timeout=2.0)

    with TestClient(app) as client:
        checks = client.get("/health/ready").json()["checks"]

    assert checks["audit_delivery"]["status"] == "error"
    assert "failed=1" in checks["audit_delivery"]["detail"]
    assert "queue_depth=0" in checks["audit_delivery"]["detail"]


def test_a_lossy_delivery_stage_can_never_leave_readiness_reported_as_ok() -> None:
    """R8: the overall verdict degrades on audit loss, and only on real loss."""
    healthy = {"database": DependencyStatus(status="ok")}
    assert determine_readiness_status(healthy) == "ok"

    healthy["audit_delivery"] = DependencyStatus(status="ok", detail="failed=0")
    assert determine_readiness_status(healthy) == "ok"

    healthy["audit_delivery"] = DependencyStatus(status="error", detail="failed=1")
    assert determine_readiness_status(healthy) == "degraded"


async def test_the_health_block_tracks_the_backlog_still_waiting_for_the_worker() -> None:
    """R8: ``queue_depth`` is the live backlog, not a value fixed at startup."""
    gate = asyncio.Event()
    queue = AuditDeliveryQueue(_GatedWriter(gate, []), base_delay_seconds=0, max_delay_seconds=0)
    bootstrap = SimpleNamespace(audit_delivery_queue=queue)
    assert audit_delivery_health(bootstrap).queue_depth == 0

    assert queue.submit(_record("audit-in-flight")) is True
    await asyncio.sleep(0)
    assert queue.submit(_record("audit-backlog")) is True

    health = audit_delivery_health(bootstrap)
    assert health.queue_depth == 1
    assert health.losing_events is False
    gate.set()
    await queue.aclose(timeout=2.0)


def test_health_omits_the_delivery_block_when_no_stage_is_wired() -> None:
    """A deployment without the gateway has no stage, and claims none."""
    assert audit_delivery_health(SimpleNamespace()) is None
    assert audit_delivery_health(SimpleNamespace(audit_delivery_queue=None)) is None


async def test_the_lifespan_drains_the_queue_after_the_transport_and_before_teardown_ends() -> None:
    """R10: ordering, proven by making the transport's shutdown release the write.

    The write can only complete after ``aclose`` opened the gate, so a passing
    order pins two things at once: the drain runs after the gateway stopped
    submitting, and the audit writer is still reachable when it runs -- the
    lifespan tears nothing down after this point.
    """
    order: list[str] = []
    gate = asyncio.Event()
    writer = _GatedWriter(gate, order)
    queue = AuditDeliveryQueue(writer, base_delay_seconds=0, max_delay_seconds=0)
    app = _lifespan_app(
        secret_provider=_RecordingSecretProvider(order),
        langgraph_gateway_transport=_GateOpeningTransport(gate, order),
        audit_delivery_queue=queue,
    )

    async with service_lifespan(app):
        assert queue.submit(_record("audit-drained")) is True

    assert order == ["secret_provider", "gateway_transport", "audit_write"]
    assert writer.written == ["audit-drained"]
    assert queue.counts().delivered == 1


async def test_the_lifespan_names_every_event_the_drain_could_not_persist(caplog) -> None:
    """R10: undelivered work is logged at shutdown rather than discarded."""
    writer = _AlwaysFailingWriter()
    queue = AuditDeliveryQueue(writer, max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)
    app = _lifespan_app(audit_delivery_queue=queue)

    with caplog.at_level(logging.ERROR, logger="zeroth.service.bootstrap.lifecycle"):
        async with service_lifespan(app):
            assert queue.submit(_record("audit-lost")) is True

    assert "audit-lost" in caplog.text
    assert queue.counts().delivered == 0
    assert queue.counts().failed == 1


async def test_the_lifespan_tolerates_a_deployment_that_wired_no_delivery_stage() -> None:
    """Teardown stays a no-op when the gateway -- and so the stage -- is absent."""
    app = _lifespan_app(audit_delivery_queue=None)

    async with service_lifespan(app):
        pass

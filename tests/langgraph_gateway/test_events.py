import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from zeroth.contracts.langgraph_gateway.models import (
    GatewayCorrelation,
    GatewayEvent,
    GatewayEventStatus,
    GovernanceLevel,
    RouteDisposition,
)
from zeroth.governance.audit.delivery import AuditDeliveryQueue, DeliveryRejection
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.governance.langgraph_gateway.events import (
    AuditGatewayEventSink,
    TeeObserver,
)


def test_json_observer_extracts_only_known_identifiers_and_tracks_safe_output_metadata():
    body = b'{"run_id":"run-7","thread_id":"thread-4","assistant_id":"assistant-2","secret":"x"}'
    observer = TeeObserver("application/json", max_observation_bytes=1024)

    assert observer.observe(body[:19]) == body[:19]
    assert observer.observe(body[19:]) == body[19:]
    observer.finish()

    assert observer.identifiers == {
        "run_id": "run-7",
        "thread_id": "thread-4",
        "assistant_id": "assistant-2",
    }
    assert observer.output_size_bytes == len(body)
    assert observer.output_sha256 == hashlib.sha256(body).hexdigest()
    assert "secret" not in repr(observer)


def test_sse_observer_extracts_only_complete_json_data_frames_across_hostile_chunks():
    observer = TeeObserver("text/event-stream; charset=utf-8", max_observation_bytes=1024)

    for chunk in (
        b"event: metadata\nda",
        b'ta: {"run_id":"run-1",',
        b'"thread_id":"thread-1"}\n\n',
        b'data: {"assistant_id":"assistant-1"}',
        b"\n\n",
    ):
        assert observer.observe(chunk) == chunk
    observer.finish()

    assert observer.identifiers == {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "assistant_id": "assistant-1",
    }


def test_sse_observation_budget_is_cumulative_across_complete_frames():
    observer = TeeObserver("text/event-stream", max_observation_bytes=64)
    chunks = [f'data: {{"sequence":{index}}}\n\n'.encode() for index in range(8)]
    chunks.append(b'data: {"run_id":"must-not-parse"}\n\n')

    for chunk in chunks:
        assert observer.observe(chunk) == chunk
    observer.finish()

    full_stream = b"".join(chunks)
    assert observer.extraction_disabled is True
    assert observer.identifiers == {}
    assert observer.output_size_bytes == len(full_stream)
    assert observer.output_sha256 == hashlib.sha256(full_stream).hexdigest()


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (
            [b'data: {"run_id":"run-cr"}\r\r'],
            {"run_id": "run-cr"},
        ),
        (
            [
                b'data: {"run_id":"run-mixed",\r',
                b'\ndata: "thread_id":"thread-mixed"}\n',
                b"\n",
            ],
            {"run_id": "run-mixed", "thread_id": "thread-mixed"},
        ),
        (
            [b'data: {"assistant_id":', b'"assistant-split"}\r', b"\n\r"],
            {"assistant_id": "assistant-split"},
        ),
    ],
)
def test_sse_parser_supports_cr_lf_crlf_and_mixed_split_boundaries(chunks, expected):
    observer = TeeObserver("text/event-stream", max_observation_bytes=1024)

    for chunk in chunks:
        observer.observe(chunk)
    observer.finish()

    assert observer.identifiers == expected


def test_sse_parser_ignores_incomplete_event_without_blank_line():
    observer = TeeObserver("text/event-stream", max_observation_bytes=1024)

    observer.observe(b'data: {"run_id":"incomplete"}\r\n')
    observer.finish()

    assert observer.identifiers == {}


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", b'{"run_id":"must-not-parse"}'),
        ("application/octet-stream", b'{"run_id":"must-not-parse"}'),
    ],
)
def test_non_json_non_sse_content_is_never_parsed(content_type, body):
    observer = TeeObserver(content_type, max_observation_bytes=1024)
    observer.observe(body)
    observer.finish()

    assert observer.identifiers == {}
    assert observer.extraction_disabled is False


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b"not-json"),
        ("text/event-stream", b"data: not-json\n\n"),
        ("application/json", b'{"run_id":"' + b"x" * 64 + b'"}'),
    ],
)
def test_malformed_or_oversized_observation_disables_extraction_without_touching_bytes(
    content_type, body
):
    observer = TeeObserver(content_type, max_observation_bytes=32)

    assert observer.observe(body) == body
    observer.finish()

    assert observer.extraction_disabled is True
    assert observer.identifiers == {}
    assert observer.output_size_bytes == len(body)


class RecordingAuditRepository:
    def __init__(self):
        self.records = []
        self.attempted_ids = []

    async def write(self, record):
        self.attempted_ids.append(record.audit_id)
        if any(stored.audit_id == record.audit_id for stored in self.records):
            raise ValueError(f"audit_id {record.audit_id!r} already exists")
        self.records.append(record)
        return record


class FlakyAuditRepository(RecordingAuditRepository):
    """Fails the first ``failures`` attempts, so a retry has to reuse the identity."""

    def __init__(self, failures):
        super().__init__()
        self._remaining = failures

    async def write(self, record):
        if self._remaining > 0:
            self._remaining -= 1
            self.attempted_ids.append(record.audit_id)
            raise ConnectionError("audit write failed")
        return await super().write(record)


class BlockingAuditRepository(RecordingAuditRepository):
    """Never returns from a write, so an awaiting producer would never come back."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def write(self, record):
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class RecordingDelivery:
    """Captures what the sink hands off, without any of the delivery machinery."""

    def __init__(self, *, accept=True, raises=None):
        self.records = []
        self.accept = accept
        self.raises = raises
        self.rejections = []

    def submit(self, record):
        self.records.append(record)
        if self.raises is not None:
            raise self.raises
        return self.accept

    def reject(self, audit_id, reason):
        self.rejections.append((audit_id, reason))


def gateway_event(started, *, tenant_id="tenant-a", **overrides):
    fields = {
        "correlation": GatewayCorrelation(
            correlation_id="corr-1",
            deployment_ref="deployment-a",
            tenant_id=tenant_id,
            principal_id="user-7",
            assistant_id="assistant-2",
            thread_id="thread-4",
            run_id="run-7",
        ),
        "operation": "runs.stream",
        "disposition": RouteDisposition.GOVERNED,
        "governance_level": GovernanceLevel.ADMISSION,
        "status": GatewayEventStatus.SUCCESS,
        "started_at": started,
        "completed_at": started,
    }
    return GatewayEvent(**(fields | overrides))


@pytest.mark.asyncio
async def test_audit_sink_submits_content_free_langgraph_gateway_node_record():
    delivery = RecordingDelivery()
    principal = AuthenticatedPrincipal(
        subject="user-7",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    sink = AuditGatewayEventSink(
        RecordingAuditRepository(),
        actor_for=lambda _event: principal.to_actor(),
        delivery=delivery,
    )
    started = datetime(2026, 7, 22, 12, tzinfo=UTC)
    event = GatewayEvent(
        correlation=GatewayCorrelation(
            correlation_id="corr-1",
            deployment_ref="deployment-a",
            tenant_id="tenant-a",
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
        policy_version="sha256:policy",
        budget_spend_usd=1.5,
        budget_cap_usd=10,
        compatibility_fingerprint="sha256:openapi",
        input_sha256="a" * 64,
        input_size_bytes=12,
        output_sha256="b" * 64,
        output_size_bytes=34,
        upstream_status_code=200,
    )

    await sink.emit(event)

    [record] = delivery.records
    assert record.node_id == "langgraph.gateway"
    assert record.run_id == "run-7"
    assert record.thread_id == "thread-4"
    # R9: the tenant is threaded from the correlation, never left to the model
    # default -- "default" is the reserved tenant of the fallback retention policy.
    assert record.tenant_id == "tenant-a"
    # The identity is already on the record the delivery stage receives.
    assert record.audit_id.startswith("langgraph.gateway:")
    assert record.actor == principal.to_actor()
    assert record.input_snapshot == {}
    assert record.output_snapshot == {}
    assert record.execution_metadata == {
        "correlation_id": "corr-1",
        "assistant_id": "assistant-2",
        "operation": "runs.stream",
        "disposition": "governed",
        "governance_level": "admission",
        "policy_version": "sha256:policy",
        "budget_spend_usd": 1.5,
        "budget_cap_usd": 10.0,
        "budget_check_degraded": False,
        "compatibility_fingerprint": "sha256:openapi",
        "upstream_status_code": 200,
        "input_sha256": "a" * 64,
        "input_size_bytes": 12,
        "output_sha256": "b" * 64,
        "output_size_bytes": 34,
        "duration_ms": 0.0,
    }
    serialized = record.model_dump_json()
    assert "raw-secret-value" not in serialized
    assert "signed-context-token-value" not in serialized


async def test_emitting_a_terminal_event_never_awaits_the_audit_write():
    # R6: the producer half. The repository below never returns, so a sink that
    # still awaited the durable write could not reach the assertions at all.
    repository = BlockingAuditRepository()
    queue = AuditDeliveryQueue(repository, base_delay_seconds=0, max_delay_seconds=0)
    sink = AuditGatewayEventSink(repository, actor_for=lambda _event: None, delivery=queue)

    await asyncio.wait_for(
        sink.emit(gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC))), timeout=1.0
    )

    assert queue.counts().queued == 1
    assert queue.counts().delivered == 0
    await asyncio.wait_for(repository.started.wait(), timeout=1.0)
    report = await queue.aclose(timeout=0)
    assert report.drained is False


async def test_one_terminal_event_keeps_one_audit_id_across_every_delivery_attempt():
    # The identity trap: the id is fixed before the hand-off, so the two failed
    # attempts and the successful one all carry the same one.
    repository = FlakyAuditRepository(failures=2)
    queue = AuditDeliveryQueue(repository, base_delay_seconds=0, max_delay_seconds=0)
    sink = AuditGatewayEventSink(repository, actor_for=lambda _event: None, delivery=queue)

    await sink.emit(gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC)))
    report = await queue.aclose(timeout=1.0)

    assert report.drained is True
    assert len(repository.attempted_ids) == 3
    assert len(set(repository.attempted_ids)) == 1
    assert [record.audit_id for record in repository.records] == repository.attempted_ids[:1]


async def test_a_correlation_without_a_tenant_is_refused_instead_of_landing_on_default():
    # R9: an omitted tenant would be misattributed to the reserved "default"
    # tenant and inherit its retention TTL, so it fails at the boundary instead.
    delivery = AuditDeliveryQueue(RecordingAuditRepository())
    sink = AuditGatewayEventSink(
        RecordingAuditRepository(), actor_for=lambda _event: None, delivery=delivery
    )
    event = gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC), tenant_id="")

    await sink.emit(event)

    assert delivery.pending == 0
    assert delivery.counts().queued == 0
    assert delivery.counts().rejected == 1


async def test_a_refused_hand_off_is_counted_by_the_stage_rather_than_raised_at_the_proxy():
    # The refusal used to be raised, and the proxy's generic handler logged it
    # with ``logger.exception`` -- one full traceback per refused event, on the
    # response-completion path, exactly when the queue is saturated. The loss is
    # already on ``rejected``; the traceback added nothing but a foreign
    # exception message on an export path and latency under load.
    delivery = RecordingDelivery(accept=False)
    sink = AuditGatewayEventSink(
        RecordingAuditRepository(), actor_for=lambda _event: None, delivery=delivery
    )

    await sink.emit(gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC)))

    assert len(delivery.records) == 1


async def test_a_saturated_queue_moves_the_rejected_counter_and_raises_nothing():
    # The end-to-end shape of the same property, against the real stage: the
    # producer sees no exception and the loss is visible on the counter the
    # metrics endpoint and the readiness probe both read.
    delivery = AuditDeliveryQueue(RecordingAuditRepository(), max_queue_size=1)
    sink = AuditGatewayEventSink(
        RecordingAuditRepository(), actor_for=lambda _event: None, delivery=delivery
    )
    started = datetime(2026, 7, 22, 12, tzinfo=UTC)

    delivery.submit(
        NodeAuditRecord(
            audit_id="filler",
            run_id="run-1",
            node_id="node-1",
            graph_version_ref="graph:v1",
            deployment_ref="deployment-1",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    await sink.emit(gateway_event(started))

    assert delivery.counts().rejected == 1
    await delivery.aclose(timeout=1.0)


async def test_a_record_the_stage_will_not_account_for_is_counted_not_raised():
    # R7/R9: an event whose tenant the stage refuses is counted as an invalid
    # record by ``submit`` itself, and the sink returns rather than handing the
    # producer an exception it has no channel to act on.
    delivery = AuditDeliveryQueue(RecordingAuditRepository())
    sink = AuditGatewayEventSink(
        RecordingAuditRepository(), actor_for=lambda _event: None, delivery=delivery
    )

    await sink.emit(gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC), tenant_id="   "))

    assert delivery.counts().queued == 0
    assert delivery.counts().rejected == 1
    await delivery.aclose(timeout=1.0)


async def test_an_event_that_cannot_be_projected_is_counted_as_a_projection_failure():
    # Everything before ``submit`` used to propagate into the proxy's
    # ``logger.exception``; now it is one counted rejection and no traceback.
    delivery = RecordingDelivery()

    def _exploding_actor(_event):
        raise RuntimeError("actor resolution failed")

    sink = AuditGatewayEventSink(
        RecordingAuditRepository(), actor_for=_exploding_actor, delivery=delivery
    )

    await sink.emit(gateway_event(datetime(2026, 7, 22, 12, tzinfo=UTC)))

    assert delivery.records == []
    [(audit_id, reason)] = delivery.rejections
    assert audit_id.startswith("langgraph.gateway:")
    assert reason is DeliveryRejection.PROJECTION_FAILED


async def test_the_sink_owns_a_bounded_delivery_stage_when_none_is_injected():
    # The production wiring passes a repository and nothing else; falling back to
    # an inline write there would restore the blocking path this stage removes.
    sink = AuditGatewayEventSink(RecordingAuditRepository(), actor_for=lambda _event: None)

    assert isinstance(sink.delivery, AuditDeliveryQueue)

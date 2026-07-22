import hashlib
from datetime import UTC, datetime

import pytest

from zeroth.core.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole
from zeroth.core.langgraph_gateway.events import (
    AuditGatewayEventSink,
    TeeObserver,
)
from zeroth.core.langgraph_gateway.models import (
    GatewayCorrelation,
    GatewayEvent,
    GatewayEventStatus,
    GovernanceLevel,
    RouteDisposition,
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

    async def write(self, record):
        self.records.append(record)
        return record


@pytest.mark.asyncio
async def test_audit_sink_writes_content_free_langgraph_gateway_node_record():
    repository = RecordingAuditRepository()
    principal = AuthenticatedPrincipal(
        subject="user-7",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    sink = AuditGatewayEventSink(repository, actor_for=lambda _event: principal.to_actor())
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

    [record] = repository.records
    assert record.node_id == "langgraph.gateway"
    assert record.run_id == "run-7"
    assert record.thread_id == "thread-4"
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

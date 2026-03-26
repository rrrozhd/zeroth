from __future__ import annotations

from datetime import UTC, datetime

from zeroth.audit import (
    AuditQuery,
    AuditRedactionConfig,
    AuditRepository,
    AuditTimelineAssembler,
    MemoryAccessRecord,
    NodeAuditRecord,
    PayloadSanitizer,
    ToolCallRecord,
)


def _record(
    *, audit_id: str, run_id: str, node_id: str, deployment_ref: str = "deploy"
) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        thread_id="thread-1",
        node_id=node_id,
        node_version=1,
        graph_version_ref="graph:v1",
        deployment_ref=deployment_ref,
        attempt=1,
        status="completed",
        input_snapshot={"secret": "hidden", "value": 1},
        output_snapshot={"token": "abc", "value": 2},
        tool_calls=[
            ToolCallRecord(
                tool_ref="tool://search",
                alias="search",
                arguments={"query": "test"},
                outcome={"result": "ok"},
            )
        ],
        memory_interactions=[
            MemoryAccessRecord(
                memory_ref="memory://thread",
                connector_type="thread",
                scope="thread",
                operation="read",
                key="latest",
                value={"value": 1},
            )
        ],
        started_at=datetime(2026, 3, 19, tzinfo=UTC),
        completed_at=datetime(2026, 3, 19, 0, 0, 1, tzinfo=UTC),
    )


def test_audit_repository_writes_queries_and_assembles_timeline(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    first = _record(audit_id="audit:1", run_id="run-1", node_id="start")
    second = _record(audit_id="audit:2", run_id="run-1", node_id="finish")
    third = _record(audit_id="audit:3", run_id="run-2", node_id="start", deployment_ref="deploy-2")

    repository.write(first)
    repository.write(second)
    repository.write(third)

    assert [record.audit_id for record in repository.list_by_run("run-1")] == ["audit:1", "audit:2"]
    assert [record.audit_id for record in repository.list_by_thread("thread-1")] == [
        "audit:1",
        "audit:2",
        "audit:3",
    ]
    assert [record.audit_id for record in repository.list_by_node("start")] == [
        "audit:1",
        "audit:3",
    ]
    assert [record.audit_id for record in repository.list_by_graph_version("graph:v1")] == [
        "audit:1",
        "audit:2",
        "audit:3",
    ]
    assert [record.audit_id for record in repository.list_by_deployment("deploy")] == [
        "audit:1",
        "audit:2",
    ]

    timeline = AuditTimelineAssembler().assemble(repository.list(AuditQuery(run_id="run-1")))
    assert [entry.audit_id for entry in timeline.entries] == ["audit:1", "audit:2"]
    assert timeline.run_id == "run-1"


def test_payload_sanitizer_redacts_keys_and_omits_fields() -> None:
    sanitizer = PayloadSanitizer(
        AuditRedactionConfig(redact_keys={"secret", "token"}, omit_paths={("nested", "remove_me")})
    )

    payload = {
        "secret": "top-secret",
        "nested": {"remove_me": "gone", "keep": "ok"},
        "token": "abc",
    }

    assert sanitizer.sanitize(payload) == {
        "secret": "***REDACTED***",
        "nested": {"keep": "ok"},
        "token": "***REDACTED***",
    }

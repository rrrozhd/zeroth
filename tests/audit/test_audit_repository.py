from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from zeroth.audit import (
    AuditContinuityVerifier,
    AuditQuery,
    AuditRedactionConfig,
    AuditRepository,
    AuditTimelineAssembler,
    MemoryAccessRecord,
    NodeAuditRecord,
    PayloadSanitizer,
    ToolCallRecord,
)
from zeroth.identity import ActorIdentity, AuthMethod, ServiceRole


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


async def test_audit_repository_writes_queries_and_assembles_timeline(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    first = _record(audit_id="audit:1", run_id="run-1", node_id="start")
    second = _record(audit_id="audit:2", run_id="run-1", node_id="finish")
    third = _record(audit_id="audit:3", run_id="run-2", node_id="start", deployment_ref="deploy-2")

    await repository.write(first)
    await repository.write(second)
    await repository.write(third)

    assert [record.audit_id for record in await repository.list_by_run("run-1")] == [
        "audit:1",
        "audit:2",
    ]
    assert [record.audit_id for record in await repository.list_by_thread("thread-1")] == [
        "audit:1",
        "audit:2",
        "audit:3",
    ]
    assert [record.audit_id for record in await repository.list_by_node("start")] == [
        "audit:1",
        "audit:3",
    ]
    assert [record.audit_id for record in await repository.list_by_graph_version("graph:v1")] == [
        "audit:1",
        "audit:2",
        "audit:3",
    ]
    assert [record.audit_id for record in await repository.list_by_deployment("deploy")] == [
        "audit:1",
        "audit:2",
    ]

    timeline = AuditTimelineAssembler().assemble(await repository.list(AuditQuery(run_id="run-1")))
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


async def test_audit_repository_round_trips_actor_and_scope(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    record = _record(audit_id="audit:scope", run_id="run-scope", node_id="scope-node").model_copy(
        update={
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-1",
            "actor": ActorIdentity(
                subject="reviewer-1",
                auth_method=AuthMethod.API_KEY,
                roles=[ServiceRole.REVIEWER],
                tenant_id="tenant-a",
                workspace_id="workspace-1",
            ),
        }
    )

    persisted = await repository.write(record)

    assert persisted.tenant_id == "tenant-a"
    assert persisted.workspace_id == "workspace-1"
    assert persisted.actor is not None
    assert persisted.actor.subject == "reviewer-1"


async def test_audit_repository_assigns_digest_chain_and_rejects_duplicate_ids(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    first = await repository.write(_record(audit_id="audit:1", run_id="run-chain", node_id="start"))
    second = await repository.write(
        _record(audit_id="audit:2", run_id="run-chain", node_id="finish")
    )

    assert first.record_digest
    assert first.previous_record_digest is None
    assert second.record_digest
    assert second.previous_record_digest == first.record_digest

    with pytest.raises(ValueError, match="audit_id"):
        await repository.write(_record(audit_id="audit:1", run_id="run-chain", node_id="duplicate"))


async def test_audit_continuity_verifier_detects_tampering_and_preserves_supersession(
    sqlite_db,
) -> None:
    repository = AuditRepository(sqlite_db)
    await repository.write(_record(audit_id="audit:1", run_id="run-verify", node_id="start"))
    await repository.write(
        _record(audit_id="audit:2", run_id="run-verify", node_id="finish").model_copy(
            update={"supersedes_audit_id": "audit:1"}
        )
    )

    verifier = AuditContinuityVerifier(repository)
    report = await verifier.verify_run("run-verify")
    assert report.verified is True
    assert report.record_count == 2
    assert report.failed_audit_id is None

    tampered = await repository.get("audit:2")
    assert tampered is not None
    tampered_payload = tampered.model_copy(update={"status": "tampered"}).model_dump(mode="json")
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (json.dumps(tampered_payload, sort_keys=True), "audit:2"),
        )

    tampered_report = await verifier.verify_run("run-verify")
    assert tampered_report.verified is False
    assert tampered_report.failed_audit_id == "audit:2"

"""What the runtime persists when nobody configured it to redact anything.

:class:`~zeroth.runtime.orchestration.audit_recorder.RuntimeAuditRecorder` is
the runtime's only route into audit storage, and its ``redact`` is a
pass-through whenever ``secret_resolver`` is ``None`` -- which is the default,
and the shape every one of these tests builds. Before the durable write became
the capture boundary that meant the four record paths below (a completed node's
history, a failed execution, a policy denial, a failed branch) wrote the node's
prompt, its answer and the exception's text to storage verbatim, and R3, R4 and
R5 were true only of the gateway's delivery queue.

Each test seeds the same credential somewhere a producer really puts one and
asserts it is absent from the *stored* record -- not from what the recorder
built, which is exactly the distinction that made the old tests pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from zeroth.contracts.graph import AgentNode, AgentNodeData
from zeroth.governance.audit.capture_policy import CAPTURE_METADATA_KEY
from zeroth.governance.audit.repository import AuditRepository
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.runs import Run
from zeroth.platform.storage import ScopeContext

SECRET = "sk-proj-SEEDED-RUNTIME-PROBE-71ca09"


def _run() -> Run:
    return Run(
        run_id="run-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        thread_id="thread-1",
        current_node_ids=[],
        pending_node_ids=[],
    )


def _node() -> AgentNode:
    return AgentNode(
        node_id="node-1",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(instruction="i", model_provider="provider://p"),
    )


def _recorder(sqlite_db: Any) -> RuntimeAuditRecorder:
    """The default runtime shape: an audit repository and no secret resolver."""
    return RuntimeAuditRecorder(
        audit_repository=AuditRepository.scoped(
            sqlite_db,
            ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
        )
    )


async def _stored(recorder: RuntimeAuditRecorder) -> list[Any]:
    assert recorder.audit_repository is not None
    return await recorder.audit_repository.list_by_run("run-1")


async def test_a_completed_node_audit_carries_no_prompt_without_a_configured_resolver(
    sqlite_db: Any,
) -> None:
    recorder = _recorder(sqlite_db)
    assert recorder.secret_resolver is None
    assert recorder.redact({"api_key": SECRET}) == {"api_key": SECRET}

    await recorder.record_history(
        _run(),
        _node(),
        "node-1",
        {"prompt": f"use {SECRET}"},
        {"answer": SECRET},
        {"model": "gpt-4o", "api_key": SECRET},
        started_at=datetime(2026, 7, 25, tzinfo=UTC),
    )

    [record] = await _stored(recorder)
    assert SECRET not in record.model_dump_json()
    assert record.input_snapshot == {}
    assert record.output_snapshot == {}
    assert record.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False


async def test_a_failed_execution_audit_carries_neither_its_input_nor_its_error_text(
    sqlite_db: Any,
) -> None:
    recorder = _recorder(sqlite_db)

    await recorder.record_failed_execution(
        _run(),
        _node(),
        "node-1",
        {"prompt": SECRET},
        RuntimeError(f"provider refused {SECRET}"),
    )

    [record] = await _stored(recorder)
    assert SECRET not in record.model_dump_json()
    assert record.status == "failed"
    assert record.error == "***REDACTED***"


async def test_a_policy_denial_audit_carries_no_denied_payload(sqlite_db: Any) -> None:
    recorder = _recorder(sqlite_db)

    await recorder.record_policy_rejection(
        _run(),
        _node(),
        {"prompt": SECRET},
        {"capability": "secrets.read", "token": SECRET},
        "capability denied",
    )

    [record] = await _stored(recorder)
    assert SECRET not in record.model_dump_json()
    assert record.status == "rejected"


async def test_a_failed_branch_audit_carries_no_branch_payload(sqlite_db: Any) -> None:
    recorder = _recorder(sqlite_db)

    await recorder.record_failed_branch_execution(
        _run(),
        _node(),
        "node-1",
        {"prompt": SECRET},
        RuntimeError(f"branch failed on {SECRET}"),
        BranchContext(branch_id="branch-1", branch_index=0, input_payload={}),
    )

    [record] = await _stored(recorder)
    assert SECRET not in record.model_dump_json()
    assert record.input_snapshot == {}


async def test_a_tool_call_promoted_to_a_typed_column_keeps_its_identity_not_its_arguments(
    sqlite_db: Any,
) -> None:
    # ``typed_fields`` promotes the runner's tool calls into queryable columns,
    # so the arguments a tool was called with reached storage through a second
    # channel that the producer-side redaction was equally absent from.
    recorder = _recorder(sqlite_db)

    await recorder.record_history(
        _run(),
        _node(),
        "node-1",
        {},
        {},
        {
            "extra": {
                "tool_calls": [
                    {
                        "tool": {"tool_ref": "tool:http", "alias": "http"},
                        "arguments": {"authorization": f"Bearer {SECRET}"},
                        "outcome": {"body": SECRET},
                    }
                ]
            }
        },
    )

    [record] = await _stored(recorder)
    assert SECRET not in record.model_dump_json()
    [call] = record.tool_calls
    assert call.tool_ref == "tool:http"
    assert call.arguments == {}
    assert call.outcome is None

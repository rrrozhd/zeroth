"""The runtime's audit recording collaborator.

``RuntimeAuditRecorder`` owns every write to the audit repository the
orchestration runtime makes: completed node history, failed and rejected node
executions, and the branch-scoped variants of both. It receives its two
dependencies explicitly — the audit repository and the secret resolver used for
redaction — rather than reading them off a mutable orchestrator.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from zeroth.contracts.graph import AgentNode, AgentNodeData
from zeroth.governance.audit.models import MemoryAccessRecord, ToolCallRecord
from zeroth.integrations.execution import ExecutableUnitAdmissionError
from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.agents.errors import AgentContentBlockedError
from zeroth.runtime.orchestration import RuntimeAuditRecorder
from zeroth.runtime.orchestration.parallel_executor import sum_run_cost
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.runs import Run
from zeroth.runtime.subgraphs.errors import SubgraphExecutionError


class _CollectingAuditRepository:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def write(self, record: Any) -> Any:
        self.records.append(record)
        return record


class _StubRedactor:
    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ("***" if k == "secret" else self.redact(v)) for k, v in value.items()}
        return value


class _StubSecretResolver:
    def redactor(self) -> _StubRedactor:
        return _StubRedactor()


def _node() -> AgentNode:
    return AgentNode(
        node_id="n1",
        graph_version_ref="g:v1",
        agent=AgentNodeData(instruction="i", model_provider="provider://p"),
    )


def _run() -> Run:
    return Run(
        graph_version_ref="g:v1",
        deployment_ref="d",
        thread_id="t",
        current_node_ids=[],
        pending_node_ids=[],
        metadata={},
    )


def test_the_recorder_takes_its_dependencies_by_injection() -> None:
    """The collaborator is constructible from explicit dependencies alone."""
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(
        audit_repository=repository,
        secret_resolver=_StubSecretResolver(),
    )

    assert recorder.audit_repository is repository
    # Both dependencies are optional: an unconfigured runtime records nothing
    # and redacts nothing, which is the pre-existing no-audit-repository path.
    bare = RuntimeAuditRecorder()
    assert bare.audit_repository is None
    assert bare.secret_resolver is None


def test_redaction_is_a_no_op_without_a_secret_resolver() -> None:
    assert RuntimeAuditRecorder().redact({"secret": "hunter2"}) == {"secret": "hunter2"}


def test_redaction_delegates_to_the_resolver_redactor() -> None:
    recorder = RuntimeAuditRecorder(secret_resolver=_StubSecretResolver())

    assert recorder.redact({"secret": "hunter2", "keep": 1}) == {"secret": "***", "keep": 1}


def test_stored_audit_ids_are_namespaced_by_run() -> None:
    assert RuntimeAuditRecorder.stored_audit_id("run-1", "audit:2") == "run-1:audit:2"


def test_typed_fields_promotes_tool_calls_and_memory_interactions() -> None:
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {"executable_unit_ref": "eu://x", "alias": "x"},
                    "arguments": {"a": 1},
                    "outcome": {"ok": True},
                    "operation_key": "op-1",
                    "operation_state": "completed",
                    "operation_replay_suppressed": True,
                }
            ],
            "memory_interactions": [
                {
                    "memory_ref": "m",
                    "connector_type": "inmemory",
                    "scope": "run",
                    "operation": "read",
                    "key": "k",
                }
            ],
        }
    }

    tool_calls, memory = RuntimeAuditRecorder.typed_fields(record)

    assert [type(tc) for tc in tool_calls] == [ToolCallRecord]
    assert tool_calls[0].tool_ref == "eu://x"
    assert tool_calls[0].operation_key == "op-1"
    assert tool_calls[0].operation_state == "completed"
    assert tool_calls[0].operation_replay_suppressed is True
    assert [type(mi) for mi in memory] == [MemoryAccessRecord]


async def test_record_history_writes_one_redacted_audit_and_appends_history() -> None:
    """History recording numbers the audit ref, redacts, and writes exactly once."""
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(
        audit_repository=repository,
        secret_resolver=_StubSecretResolver(),
    )
    run = _run()

    await recorder.record_history(
        run,
        _node(),
        "n1",
        {"secret": "hunter2"},
        {"out": 1},
        {"cost_usd": 0.5},
    )

    assert run.audit_refs == ["audit:1"]
    (record,) = repository.records
    assert record.audit_id == f"{run.run_id}:audit:1"
    assert record.status == "completed"
    assert record.input_snapshot == {"secret": "***"}
    assert record.cost_usd == 0.5
    assert [entry.node_id for entry in run.execution_history] == ["n1"]
    assert run.execution_history[0].audit_ref == "audit:1"


async def test_record_history_without_a_repository_still_tracks_refs() -> None:
    """The no-audit-repository path still numbers refs and appends history."""
    recorder = RuntimeAuditRecorder()
    run = _run()

    await recorder.record_history(run, _node(), "n1", {}, {"out": 1}, {})

    assert run.audit_refs == ["audit:1"]
    assert [entry.node_id for entry in run.execution_history] == ["n1"]


async def test_failed_execution_records_a_normalized_reason_code_for_a_bare_error() -> None:
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(audit_repository=repository)
    run = _run()

    await recorder.record_failed_execution(run, _node(), "n1", {}, ValueError("boom"))

    (record,) = repository.records
    assert record.status == "failed"
    assert record.execution_metadata == {"reason_code": "value_error"}
    assert record.error == "boom"


@pytest.mark.parametrize(
    "error",
    [
        AgentContentBlockedError(
            "blocked",
            direction="output",
            findings=["unsafe"],
            audit_record={"guardrail": "content_safety", "cost_usd": 0.25},
        ),
        ExecutableUnitAdmissionError(
            "denied",
            audit_record={"admitted": False, "cost_usd": 0.25},
        ),
    ],
)
async def test_failed_execution_preserves_explicit_governance_rejections(
    error: Exception,
) -> None:
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(audit_repository=repository)
    run = _run()
    await recorder.record_failed_execution(run, _node(), "n1", {}, error)

    (record,) = repository.records
    assert record.status == "rejected"
    assert record.cost_usd == 0.25


async def test_failed_execution_appends_cost_history_without_a_repository() -> None:
    recorder = RuntimeAuditRecorder()
    run = _run()
    error = SubgraphExecutionError("child failed")
    error.audit_record = {  # type: ignore[attr-defined]
        "estimated_cost_usd": 0.25,
        "cost_measurement": MeasurementState.ESTIMATED,
    }

    await recorder.record_failed_execution(run, _node(), "n1", {}, error)

    (history,) = run.execution_history
    assert history.status == "failed"
    assert history.estimated_cost_usd == 0.25
    assert history.cost_measurement is MeasurementState.ESTIMATED
    assert sum_run_cost(run) == 0.25


async def test_failed_execution_merges_carried_operation_facts() -> None:
    """ZER26-AUD-008: a timeout's operation facts must reach the durable record.

    The dispatcher attaches them as ``operation_audit`` on the exception; the
    recorder previously read only ``audit_record``, so the durable audit of a
    timed-out side effect — the outcome whose record matters most — carried no
    operation state at all.
    """
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(audit_repository=repository)
    run = _run()
    error = TimeoutError("no receipt within deadline")
    error.operation_audit = {  # type: ignore[attr-defined]
        "operation_key": "op-1",
        "operation_state": "ambiguous",
        "operation_residual_duplicate_risk": True,
    }

    await recorder.record_failed_execution(run, _node(), "n1", {}, error)

    (record,) = repository.records
    assert record.status == "failed", "operation facts alone are not a governance rejection"
    assert record.execution_metadata["operation_key"] == "op-1"
    assert record.execution_metadata["operation_state"] == "ambiguous"
    assert record.execution_metadata["operation_residual_duplicate_risk"] is True


async def test_failed_branch_execution_uses_the_branch_audit_namespace() -> None:
    repository = _CollectingAuditRepository()
    recorder = RuntimeAuditRecorder(audit_repository=repository)
    run = _run()
    ctx = BranchContext(branch_index=2, branch_id="b2", input_payload={})

    await recorder.record_failed_branch_execution(run, _node(), "n1", {}, ValueError("boom"), ctx)

    (record,) = repository.records
    assert record.audit_id == f"{run.run_id}:branch:2:audit:1"
    assert ctx.audit_refs == [f"{run.run_id}:branch:2:audit:1"]
    assert record.execution_metadata["branch_index"] == 2
    assert record.execution_metadata["branch_id"] == "b2"
    # Branch refs never consume a parent audit:N slot.
    assert run.audit_refs == []


async def test_failed_branch_execution_appends_cost_history_without_a_repository() -> None:
    recorder = RuntimeAuditRecorder()
    run = _run()
    ctx = BranchContext(branch_index=2, branch_id="b2", input_payload={})
    error = SubgraphExecutionError("child failed")
    error.audit_record = {  # type: ignore[attr-defined]
        "cost_usd": 0.5,
        "cost_measurement": MeasurementState.MEASURED,
    }

    await recorder.record_failed_branch_execution(run, _node(), "n1", {}, error, ctx)

    (history,) = ctx.execution_history
    assert history.status == "failed"
    assert history.cost_usd == 0.5
    assert history.cost_measurement is MeasurementState.MEASURED


@pytest.mark.parametrize(
    "statement",
    [
        "from zeroth.runtime.orchestration import RuntimeAuditRecorder",
    ],
)
def test_the_package_imports_in_a_cold_interpreter(statement: str) -> None:
    """Both import directions must work from a cold interpreter.

    ``tests/conftest.py`` imports service bootstrap at collection time, so the
    in-process suite structurally cannot see an import cycle between the legacy
    orchestrator module and the canonical runtime package. Only a subprocess
    can exercise both cold-import directions reliably.
    """
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

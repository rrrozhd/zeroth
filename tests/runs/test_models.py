from __future__ import annotations

from datetime import UTC

import zeroth.core.runs.models as run_models
from zeroth.contracts.governed import RunStatus

from zeroth.runtime.runs import Run, RunFailureState, RunHistoryEntry, Thread, ThreadMemoryBinding
from zeroth.contracts.conditions.models import RunConditionResult
from zeroth.runtime.runs import ThreadStatus
from zeroth.platform.primitives import utc_now


def test_run_models_consume_platform_clock_per_instance() -> None:
    assert run_models.RunHistoryEntry.model_fields["started_at"].default_factory is utc_now
    assert run_models.RunConditionResult.model_fields["evaluated_at"].default_factory is utc_now
    assert run_models.Thread.model_fields["created_at"].default_factory is utc_now
    assert run_models.Thread.model_fields["updated_at"].default_factory is utc_now

    first = run_models.RunHistoryEntry(node_id="node-1", status="started")
    second = run_models.RunHistoryEntry(node_id="node-2", status="started")

    assert first.started_at.tzinfo is UTC
    assert first.started_at is not second.started_at


def test_run_model_round_trips_json_serialization() -> None:
    run = Run(
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        workflow_name="workflow:v1",
        current_node_ids=["node-a"],
        pending_node_ids=["node-b"],
        execution_history=[RunHistoryEntry(node_id="node-a", status="done")],
        node_visit_counts={"node-a": 2},
        condition_results=[
            RunConditionResult(condition_id="cond-1", matched=True, selected_edge_id="edge-1")
        ],
        audit_refs=["audit-1"],
        final_output={"result": "ok"},
        failure_state=RunFailureState(reason="none", message=None),
    )

    assert Run.model_validate(run.model_dump(mode="json")) == run
    assert run.thread_id == run.run_id
    assert run.status is RunStatus.PENDING
    assert run.current_step == "node-a"
    assert run.completed_steps == ["node-a"]


def test_thread_model_round_trips_json_serialization() -> None:
    thread = Thread(
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        participating_agent_refs=["agent-a"],
        state_snapshot_refs=["snapshot-a"],
        checkpoint_refs=["checkpoint-a"],
        memory_bindings=[ThreadMemoryBinding(connector_id="memory", instance_id="instance")],
        run_ids=["run-a"],
        active_run_id="run-a",
        last_run_id="run-a",
    )

    assert Thread.model_validate(thread.model_dump(mode="json")) == thread
    assert thread.status is ThreadStatus.ACTIVE


def test_run_status_enum_covers_phase_states() -> None:
    assert {status.value for status in RunStatus} == {
        "PENDING",
        "RUNNING",
        "WAITING_APPROVAL",
        "WAITING_INTERRUPT",
        "COMPLETED",
        "FAILED",
    }

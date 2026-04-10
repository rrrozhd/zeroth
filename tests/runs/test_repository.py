from __future__ import annotations

import pytest
from governai import RunStatus

from zeroth.core.runs.models import (
    Run,
    RunFailureState,
    RunHistoryEntry,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)
from zeroth.core.runs.repository import RunRepository, ThreadRepository


async def test_run_repository_crud_round_trip(runs_db) -> None:
    repo = RunRepository(runs_db)
    run = await repo.create(
        Run(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
            workflow_name="workflow:v1",
            current_node_ids=["node-a"],
            pending_node_ids=["node-b"],
            execution_history=[RunHistoryEntry(node_id="node-a", status="started")],
            node_visit_counts={"node-a": 1},
            audit_refs=["audit-1"],
        )
    )

    loaded = await repo.get(run.run_id)

    assert loaded == run
    assert loaded.thread_id == loaded.run_id
    assert loaded.workflow_name == "workflow:v1"


async def test_run_repository_transitions_and_validation(runs_db) -> None:
    repo = RunRepository(runs_db)
    run = await repo.create(
        Run(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    )

    running = await repo.transition(run.run_id, RunStatus.RUNNING, current_node_ids=["node-a"])
    waiting = await repo.transition(running.run_id, RunStatus.WAITING_APPROVAL)
    succeeded = await repo.transition(
        waiting.run_id,
        RunStatus.COMPLETED,
        final_output={"status": "ok"},
    )

    assert running.status is RunStatus.RUNNING
    assert running.current_node_ids == ["node-a"]
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert succeeded.status is RunStatus.COMPLETED
    assert succeeded.final_output == {"status": "ok"}

    with pytest.raises(ValueError, match="invalid run transition"):
        await repo.transition(succeeded.run_id, RunStatus.RUNNING)


async def test_run_repository_terminal_failure_state_round_trip(runs_db) -> None:
    repo = RunRepository(runs_db)
    run = await repo.create(
        Run(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    )

    failed = await repo.transition(
        run.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(reason="bad-input", message="invalid payload"),
    )

    assert failed.status is RunStatus.FAILED
    assert failed.failure_state == RunFailureState(reason="bad-input", message="invalid payload")


async def test_run_repository_checkpoint_semantics(runs_db) -> None:
    repo = RunRepository(runs_db)
    run = await repo.create(
        Run(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    )

    checkpoint_id = await repo.write_checkpoint(run)
    latest = await repo.get_latest_checkpoint(run.thread_id)

    assert checkpoint_id
    assert latest is not None
    assert latest.run_id == run.run_id
    assert await repo.list_checkpoints(run.thread_id)


async def test_thread_repository_create_and_continue(runs_db) -> None:
    repo = ThreadRepository(runs_db)
    created = await repo.resolve(
        None,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        participating_agent_refs=["agent-a"],
        state_snapshot_refs=["snapshot-a"],
        checkpoint_refs=["checkpoint-a"],
        memory_bindings=[ThreadMemoryBinding(connector_id="memory", instance_id="instance")],
        run_id="run-a",
    )
    continued = await repo.resolve(
        created.thread_id,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        participating_agent_refs=["agent-b"],
        state_snapshot_refs=["snapshot-b"],
        checkpoint_refs=["checkpoint-b"],
        run_id="run-b",
    )

    assert created.thread_id == continued.thread_id
    assert continued.participating_agent_refs == ["agent-a", "agent-b"]
    assert continued.state_snapshot_refs == ["snapshot-a", "snapshot-b"]
    assert continued.checkpoint_refs == ["checkpoint-a", "checkpoint-b"]
    assert continued.run_ids == ["run-a", "run-b"]
    assert continued.last_run_id == "run-b"
    assert continued.active_run_id == "run-b"
    assert continued.status is ThreadStatus.ACTIVE


async def test_thread_repository_attach_run_updates_existing_thread(runs_db) -> None:
    repo = ThreadRepository(runs_db)
    thread = await repo.create(
        Thread(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    )

    updated = await repo.attach_run(thread.thread_id, "run-a")

    assert updated.run_ids == ["run-a"]
    assert updated.active_run_id == "run-a"
    assert updated.last_run_id == "run-a"


async def test_thread_repository_thread_aware_run_indexing(runs_db) -> None:
    repo = ThreadRepository(runs_db)
    thread = await repo.create(
        Thread(
            graph_version_ref="graph:v1",
            deployment_ref="deployment:v1",
        )
    )

    await repo.set_active_run_id(thread.thread_id, "run-a")

    assert await repo.get_active_run_id(thread.thread_id) == "run-a"
    assert await repo.get_latest_run_id(thread.thread_id) == "run-a"
    assert await repo.list_run_ids(thread.thread_id) == ["run-a"]
    # Verify active run was set (clear_active_run_id removed in async rewrite)
    assert await repo.get_active_run_id(thread.thread_id) == "run-a"

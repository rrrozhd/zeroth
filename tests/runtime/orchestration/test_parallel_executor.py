"""The runtime's fan-out / fan-in collaborator.

``RuntimeParallelExecutor`` owns the parallel path: splitting a node's output
into branches, running the downstream nodes of each branch concurrently with
branch-isolated state, and merging the results back into the parent run. It
also owns the D-11 approval pause — persisting enough state that a fan-out
interrupted by an approval inside a subgraph branch resumes without
re-executing the siblings that already finished.

The end-to-end behavioral guard is ``tests/parallel`` and ``tests/subgraph``.
These tests pin the collaborator boundary and the pause/resume state shape,
which is what a decomposition can silently change.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.orchestration import RuntimeParallelExecutor
from zeroth.runtime.parallel.models import BranchResult, FanInResult
from zeroth.runtime.runs import Run, RunFailureState, RunHistoryEntry, RunStatus
from zeroth.runtime.subgraphs.errors import SubgraphExecutionError


class _EchoRunRepository:
    def __init__(self) -> None:
        self.puts: list[Run] = []
        self.checkpoints: list[Run] = []

    async def put(self, run: Run) -> Run:
        self.puts.append(run)
        return run

    async def write_checkpoint(self, run: Run) -> str:
        self.checkpoints.append(run)
        return "cp"


def _run(**kwargs: Any) -> Run:
    defaults: dict[str, Any] = {
        "graph_version_ref": "g:v1",
        "deployment_ref": "d",
        "thread_id": "t",
        "current_node_ids": [],
        "pending_node_ids": [],
        "metadata": {},
    }
    defaults.update(kwargs)
    return Run(**defaults)


def _executor(**overrides: Any) -> RuntimeParallelExecutor:
    kwargs: dict[str, Any] = {
        "run_repository": _EchoRunRepository(),
        "refresh_artifact_ttls": _noop,
    }
    kwargs.update(overrides)
    return RuntimeParallelExecutor(**kwargs)


async def _noop(run: Run) -> None:
    return None


def test_the_executor_takes_its_dependencies_by_injection() -> None:
    """Every collaborator arrives explicitly; the optional ones default off."""
    repository = _EchoRunRepository()
    executor = _executor(run_repository=repository)

    assert executor.run_repository is repository
    assert executor.subgraph_executor is None
    assert executor.budget_enforcer is None
    assert executor.policy_gate is None


def test_merging_fan_in_state_appends_branch_history_and_refs_in_order() -> None:
    """Branch history lands on the parent after its own entry, in branch order."""
    executor = _executor()
    run = _run()
    run.execution_history.append(
        RunHistoryEntry(node_id="source", status="completed", input_snapshot={}, output_snapshot={})
    )
    run.audit_refs = ["audit:1"]
    fan_in = FanInResult(
        results=[
            BranchResult(
                branch_index=0,
                output={},
                audit_refs=["r:branch:0:audit:1"],
                execution_history=[
                    RunHistoryEntry(
                        node_id="sink",
                        status="completed",
                        input_snapshot={},
                        output_snapshot={},
                    )
                ],
            ),
            BranchResult(
                branch_index=1,
                output={},
                audit_refs=["r:branch:1:audit:1"],
                execution_history=[
                    RunHistoryEntry(
                        node_id="sink",
                        status="completed",
                        input_snapshot={},
                        output_snapshot={},
                    )
                ],
            ),
        ]
    )

    executor.merge_fan_in_state(run, fan_in)

    assert [entry.node_id for entry in run.execution_history] == ["source", "sink", "sink"]
    assert run.audit_refs == ["audit:1", "r:branch:0:audit:1", "r:branch:1:audit:1"]
    assert run.completed_steps == ["source", "sink", "sink"]


async def test_a_branch_approval_pause_persists_resumable_state(monkeypatch) -> None:
    """The pause stash carries everything needed to resume without re-running.

    D-11: completed siblings are rehydrated as-is, the paused branch is the only
    child re-entered, and cancelled siblings become explicit None-output
    results. Dropping any of the three from the stash changes what resume does.
    """
    repository = _EchoRunRepository()
    executor = _executor(run_repository=repository)
    run = _run()
    fan_in = FanInResult(
        results=[],
        pause_state={
            "paused": {
                "branch_index": 1,
                "child_run_id": "child-1",
                "graph_ref": "sub",
                "version": 2,
                "node_id": "sg",
            },
            "completed_branch_results": [
                BranchResult(branch_index=0, output={"done": True}, audit_refs=["a"])
            ],
            "cancelled_branch_contexts": [
                {"branch_index": 2, "branch_id": "b2", "input_payload": {}}
            ],
            "split_input": {"items": [1, 2, 3]},
        },
    )

    paused = await executor.handle_subgraph_pause(run, object(), "source", {}, {}, fan_in)

    assert paused.status is RunStatus.WAITING_APPROVAL
    stash = paused.metadata["pending_parallel_subgraph"]
    assert stash["node_id"] == "source"
    assert stash["split_input"] == {"items": [1, 2, 3]}
    assert stash["paused_branch"]["child_run_id"] == "child-1"
    assert [b["branch_index"] for b in stash["completed_branches"]] == [0]
    assert [c["branch_index"] for c in stash["cancelled_branches"]] == [2]
    # The fan-out source is re-queued at the head so resume re-enters at it.
    assert paused.pending_node_ids[0] == "source"
    assert repository.checkpoints


async def test_resuming_without_a_subgraph_executor_is_an_orchestrator_error() -> None:
    from zeroth.runtime.orchestration import OrchestratorError

    executor = _executor()

    with pytest.raises(OrchestratorError, match="without SubgraphExecutor"):
        await executor.execute_fan_out_resume(
            object(),
            _run(),
            _ParallelNode(),
            "source",
            {"paused_branch": {"branch_index": 0, "child_run_id": "c", "graph_ref": "g"}},
            step_tracker=None,
        )


async def test_cancelled_approval_sibling_keeps_fan_in_unmeasured() -> None:
    subgraph_executor = AsyncMock()
    subgraph_executor.resume = AsyncMock(
        return_value=_run(
            status=RunStatus.COMPLETED,
            final_output={"done": True},
            metadata={"total_cost_usd": 0.5, "cost_measurement": "measured"},
        )
    )
    result = await _executor(
        subgraph_executor=subgraph_executor,
        orchestrator=object(),
    ).execute_fan_out_resume(
        object(),
        _run(),
        _ParallelNode(),
        "source",
        {
            "paused_branch": {
                "branch_index": 1,
                "child_run_id": "child-1",
                "graph_ref": "sub",
            },
            "completed_branches": [
                {
                    "branch_index": 0,
                    "output": {"done": True},
                    "cost_usd": 0.2,
                    "cost_measurement": "measured",
                }
            ],
            "cancelled_branches": [{"branch_index": 2}],
            "split_input": {"items": [1, 2, 3]},
        },
        step_tracker=None,
    )

    cancelled = result.results[2]
    assert cancelled.cost_usd is None
    assert cancelled.cost_measurement is MeasurementState.UNMEASURED
    assert result.total_cost_usd == pytest.approx(0.7)
    assert result.cost_measurement is MeasurementState.UNMEASURED


async def test_failed_resumed_parallel_child_rolls_once_and_propagates() -> None:
    subgraph_executor = AsyncMock()
    subgraph_executor.resume = AsyncMock(
        return_value=_run(
            run_id="child-1",
            status=RunStatus.FAILED,
            failure_state=RunFailureState(reason="child_failed", message="boom"),
            execution_history=[
                RunHistoryEntry(
                    node_id="paid-child",
                    status="failed",
                    cost_usd=0.3,
                    cost_measurement=MeasurementState.MEASURED,
                )
            ],
        )
    )

    with pytest.raises(RuntimeError, match="ended FAILED") as raised:
        await _executor(
            subgraph_executor=subgraph_executor,
            orchestrator=object(),
        ).execute_fan_out_resume(
            object(),
            _run(),
            _ParallelNode(),
            "source",
            {
                "paused_branch": {
                    "branch_index": 0,
                    "child_run_id": "child-1",
                    "graph_ref": "sub",
                    "node_id": "child-node",
                    "branch_context": {"branch_index": 0, "execution_history": []},
                },
                "split_input": {"items": [1]},
            },
            step_tracker=None,
        )

    assert raised.value.audit_record["cost_usd"] == pytest.approx(0.3)
    assert raised.value.audit_record["cost_measurement"] is MeasurementState.MEASURED


async def test_raised_parallel_resume_failure_settles_all_branch_histories_once() -> None:
    error = SubgraphExecutionError("resume failed after paid child work")
    error.audit_record = {  # type: ignore[attr-defined]
        "cost_usd": 0.3,
        "cost_measurement": MeasurementState.MEASURED,
    }
    subgraph_executor = AsyncMock()
    subgraph_executor.resume = AsyncMock(side_effect=error)
    run = _run()

    def history(node_id: str, audit_ref: str) -> dict[str, Any]:
        return RunHistoryEntry(
            node_id=node_id,
            status="completed",
            audit_ref=audit_ref,
        ).model_dump(mode="json")

    with pytest.raises(SubgraphExecutionError) as raised:
        await _executor(
            subgraph_executor=subgraph_executor,
            orchestrator=object(),
        ).execute_fan_out_resume(
            object(),
            run,
            _ParallelNode(),
            "source",
            {
                "paused_branch": {
                    "branch_index": 1,
                    "child_run_id": "child-1",
                    "graph_ref": "sub",
                    "node_id": "child-node",
                    "branch_context": {
                        "branch_index": 1,
                        "audit_refs": ["paused-audit"],
                        "execution_history": [history("paused-prior", "paused-audit")],
                    },
                },
                "completed_branches": [
                    {
                        "branch_index": 0,
                        "output": {"done": True},
                        "audit_refs": ["completed-audit"],
                        "execution_history": [history("completed", "completed-audit")],
                    }
                ],
                "cancelled_branches": [
                    {
                        "branch_index": 2,
                        "audit_refs": ["cancelled-audit"],
                        "execution_history": [history("cancelled-prior", "cancelled-audit")],
                    }
                ],
            },
            step_tracker=None,
        )

    assert raised.value is error
    settled = [(entry.node_id, entry.status) for entry in run.execution_history]
    assert settled.count(("completed", "completed")) == 1
    assert settled.count(("paused-prior", "completed")) == 1
    assert settled.count(("child-node", "failed")) == 1
    assert settled.count(("cancelled-prior", "completed")) == 1
    assert settled.count(("child-node", "cancelled")) == 1


class _ParallelNode:
    node_id = "source"
    parallel_config = {"split_path": "items"}


@pytest.mark.parametrize(
    "statement",
    [
        "from zeroth.runtime.orchestration import RuntimeParallelExecutor",
    ],
)
def test_the_package_imports_in_a_cold_interpreter(statement: str) -> None:
    """Both import directions must work from a cold interpreter."""
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

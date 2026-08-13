"""ZER-49 A06-6: branch contexts and branch results must be paired by identity.

The best-effort pause path used to zip a *filtered* context list against an
*unfiltered* result list with ``strict=False``. That is correct only while
every ``branch_index`` is unique AND the pause signal's index belongs to one of
this level's contexts. Neither is guaranteed: ``BranchApprovalPauseSignal`` is a
``BaseException``, so it escapes ``SubgraphExecutor``'s ``except Exception`` and
a pause raised inside a *nested* fan-out arrives here carrying the inner
branch index. When that happens ``strict=False`` silently truncates and every
context is paired with the wrong result — the paused branch gets reported as
cancelled and the branch that actually failed is dropped from the stash.

The resume half has the same shape: a cancelled-branch record with no
``branch_index`` silently became ``BranchResult(branch_index=-1)``.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.contracts.graph.models import ParallelConfig
from zeroth.runtime.orchestration import RuntimeParallelExecutor
from zeroth.runtime.parallel.errors import BranchApprovalPauseSignal, ParallelExecutionError
from zeroth.runtime.parallel.executor import ParallelExecutor
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.runs import Run, RunStatus

BEST_EFFORT = ParallelConfig(split_path="items", fail_mode="best_effort")


def _ctx(index: int) -> BranchContext:
    return BranchContext(
        branch_index=index,
        branch_id=f"run-1:branch:{index}",
        input_payload={"i": index},
    )


async def test_a_pause_index_that_is_not_ours_is_refused_not_mispaired() -> None:
    """A nested fan-out's pause index must fail loudly, never mispair."""
    contexts = [_ctx(0), _ctx(1), _ctx(2)]

    async def factory(ctx: BranchContext) -> dict[str, Any]:
        if ctx.branch_index == 1:
            raise BranchApprovalPauseSignal(
                branch_index=99,  # an index from a nested fan-out, not ours
                child_run_id="child-99",
                graph_ref="sub",
                version=1,
                node_id="sg",
            )
        if ctx.branch_index == 2:
            raise RuntimeError("boom")
        return {"ok": ctx.branch_index}

    with pytest.raises(ParallelExecutionError, match="99"):
        await ParallelExecutor().execute_branches(contexts, factory, BEST_EFFORT)


async def test_duplicate_branch_indices_are_refused() -> None:
    contexts = [_ctx(0), _ctx(1), _ctx(1)]

    async def factory(ctx: BranchContext) -> dict[str, Any]:
        if ctx.branch_index == 0:
            raise BranchApprovalPauseSignal(
                branch_index=0,
                child_run_id="child-0",
                graph_ref="sub",
                version=1,
                node_id="sg",
            )
        raise RuntimeError("boom")

    with pytest.raises(ParallelExecutionError, match="branch_index"):
        await ParallelExecutor().execute_branches(contexts, factory, BEST_EFFORT)


async def test_a_well_formed_pause_pairs_each_context_with_its_own_result() -> None:
    """Regression guard for the healthy path the zip already got right."""
    contexts = [_ctx(0), _ctx(1), _ctx(2)]

    async def factory(ctx: BranchContext) -> dict[str, Any]:
        if ctx.branch_index == 1:
            raise BranchApprovalPauseSignal(
                branch_index=1,
                child_run_id="child-1",
                graph_ref="sub",
                version=1,
                node_id="sg",
            )
        if ctx.branch_index == 2:
            raise RuntimeError("boom")
        return {"ok": ctx.branch_index}

    with pytest.raises(BranchApprovalPauseSignal) as caught:
        await ParallelExecutor().execute_branches(contexts, factory, BEST_EFFORT)

    pause = caught.value
    assert [br.branch_index for br in pause.completed_branch_results] == [0]
    assert [ctx.branch_index for ctx in pause.cancelled_branch_contexts] == [2]


async def test_a_branch_error_always_carries_a_non_empty_message() -> None:
    """``str(CancelledError())`` is empty; the folded result must still say what happened."""
    import asyncio

    contexts = [_ctx(0)]

    async def factory(ctx: BranchContext) -> dict[str, Any]:
        raise asyncio.CancelledError

    results = await ParallelExecutor().execute_branches(contexts, factory, BEST_EFFORT)

    assert results[0].error
    assert "CancelledError" in results[0].error


# ---------------------------------------------------------------------------
# Resume half: orchestration/parallel_executor.py
# ---------------------------------------------------------------------------


class _EchoRunRepository:
    async def put(self, run: Run) -> Run:
        return run

    async def write_checkpoint(self, run: Run) -> str:
        return "cp"

    async def get(self, run_id: str) -> Run | None:
        return None


class _ResumingSubgraphExecutor:
    async def resume(self, **kwargs: Any) -> Run:
        run = Run(graph_version_ref="g:v1", deployment_ref="d", thread_id="t")
        run.status = RunStatus.COMPLETED
        run.final_output = {"resumed": True}
        return run


class _ParallelNode:
    node_id = "source"
    parallel_config = {"split_path": "items"}


async def _noop(run: Run) -> None:
    return None


def _resume_executor() -> RuntimeParallelExecutor:
    return RuntimeParallelExecutor(
        run_repository=_EchoRunRepository(),
        refresh_artifact_ttls=_noop,
        subgraph_executor=_ResumingSubgraphExecutor(),
    )


async def test_a_cancelled_record_without_a_branch_index_is_refused() -> None:
    pending = {
        "completed_branches": [],
        "paused_branch": {
            "branch_index": 0,
            "child_run_id": "child-0",
            "graph_ref": "sub",
            "version": None,
        },
        "cancelled_branches": [{"branch_id": "run-1:branch:1", "input_payload": {}}],
        "split_input": {"items": [1, 2]},
    }

    with pytest.raises(ParallelExecutionError, match="branch_index"):
        await _resume_executor().execute_fan_out_resume(
            object(),
            Run(graph_version_ref="g:v1", deployment_ref="d", thread_id="t"),
            _ParallelNode(),
            "source",
            pending,
            step_tracker=None,
        )


async def test_a_well_formed_resume_still_rebuilds_every_branch() -> None:
    pending = {
        "completed_branches": [{"branch_index": 0, "output": {"done": True}, "error": None}],
        "paused_branch": {
            "branch_index": 1,
            "child_run_id": "child-1",
            "graph_ref": "sub",
            "version": None,
        },
        "cancelled_branches": [{"branch_index": 2, "branch_id": "b2", "input_payload": {}}],
        "split_input": {"items": [1, 2, 3]},
    }

    result = await _resume_executor().execute_fan_out_resume(
        object(),
        Run(graph_version_ref="g:v1", deployment_ref="d", thread_id="t"),
        _ParallelNode(),
        "source",
        pending,
        step_tracker=None,
    )

    assert [br.branch_index for br in result.results] == [0, 1, 2]
    assert result.results[2].error == "cancelled_by_approval_pause"

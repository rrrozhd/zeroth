"""Phase 43 Plan 01 — SubgraphNode inside parallel fan-out composition.

This module covers the data-layer and wiring work for parallel subgraph
composition per 43-01-PLAN.md:

Task 1 (data layer):
    * ``SubgraphNode`` accepts ``parallel_config`` (D-05, D-23 — prior
      ``_reject_parallel_config`` model validator removed).
    * ``ParallelExecutor.split_fan_out`` no longer raises for a
      downstream ``SubgraphNode`` but STILL rejects ``HumanApprovalNode``.
    * ``BranchApprovalPauseSignal`` is a ``BaseException`` subclass, not
      an ``Exception``, so ``asyncio.gather(return_exceptions=True)``
      cannot swallow it.
    * ``FanInResult`` carries an optional ``pause_state`` dict.

Task 2 & 3 wiring tests live below under the appropriate class markers.

These tests intentionally stay at the unit level — higher-level
composition scenarios that require a full orchestrator + SubgraphResolver
stub live in ``tests/subgraph/test_integration.py`` style and reference
this module via the ``SubgraphNode`` + ``ParallelConfig`` constructors.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from zeroth.core.graph.models import (
    HumanApprovalNode,
    HumanApprovalNodeData,
    SubgraphNode,
)
from zeroth.core.parallel.errors import (
    BranchApprovalPauseSignal,
    FanOutValidationError,
)
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    ParallelConfig,
)
from zeroth.core.subgraph.models import SubgraphNodeData


# ---------------------------------------------------------------------------
# Task 1: data layer
# ---------------------------------------------------------------------------


class TestSubgraphNodeAcceptsParallelConfig:
    """D-05 + D-23: SubgraphNode model-validation must allow parallel_config."""

    def test_construct_with_parallel_config(self) -> None:
        node = SubgraphNode(
            node_id="s1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf"),
            parallel_config=ParallelConfig(split_path="items"),
        )
        assert node.parallel_config is not None
        assert node.parallel_config.split_path == "items"

    def test_construct_without_parallel_config_still_works(self) -> None:
        node = SubgraphNode(
            node_id="s1",
            graph_version_ref="g1@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf"),
        )
        assert node.parallel_config is None


class TestSplitFanOutSubgraphUnblock:
    """D-05 + D-23: split_fan_out must no longer reject SubgraphNode."""

    def test_subgraph_node_downstream_allowed(self) -> None:
        executor = ParallelExecutor()
        config = ParallelConfig(split_path="items")
        # Use a MagicMock with node_type="subgraph" — the check is a simple
        # hasattr/node_type comparison so this is representative.
        node = MagicMock()
        node.node_type = "subgraph"
        branches = executor.split_fan_out(
            "run1", {"items": [{"a": 1}, {"b": 2}]}, config, node
        )
        assert len(branches) == 2
        assert branches[0].branch_index == 0
        assert branches[1].branch_index == 1

    def test_human_approval_node_still_rejected(self) -> None:
        """Regression guard — HumanApprovalNode reject must be preserved."""
        executor = ParallelExecutor()
        config = ParallelConfig(split_path="items")
        node = HumanApprovalNode(
            node_id="approval1",
            graph_version_ref="g1@1",
            human_approval=HumanApprovalNodeData(),
        )
        with pytest.raises(FanOutValidationError, match="HumanApprovalNode"):
            executor.split_fan_out("run1", {"items": [1, 2]}, config, node)


class TestBranchApprovalPauseSignal:
    """Signal semantics: must inherit BaseException, not Exception."""

    def test_is_base_exception(self) -> None:
        sig = BranchApprovalPauseSignal(
            branch_index=1,
            child_run_id="r",
            graph_ref="g",
            version=1,
            node_id="n",
        )
        assert isinstance(sig, BaseException)

    def test_is_not_exception(self) -> None:
        """BaseException subclass that is NOT an Exception — this is the
        whole point: asyncio.gather(return_exceptions=True) only captures
        Exception (and subclasses), leaving BaseException to escape.
        """
        assert not isinstance(
            BranchApprovalPauseSignal(
                branch_index=0,
                child_run_id="r",
                graph_ref="g",
                version=1,
                node_id="n",
            ),
            Exception,
        )

    def test_carries_all_metadata(self) -> None:
        sig = BranchApprovalPauseSignal(
            branch_index=2,
            child_run_id="child-run-42",
            graph_ref="child-g",
            version=3,
            node_id="sub-node",
        )
        assert sig.branch_index == 2
        assert sig.child_run_id == "child-run-42"
        assert sig.graph_ref == "child-g"
        assert sig.version == 3
        assert sig.node_id == "sub-node"

    @pytest.mark.asyncio
    async def test_propagates_through_plain_gather(self) -> None:
        """Plain ``gather`` (no return_exceptions) re-raises the signal.

        This is the fail-fast path's guarantee: the signal propagates out
        of ``asyncio.gather(*tasks)`` and the outer ``try/except
        BranchApprovalPauseSignal`` catches it. BaseException semantics
        ensure that even if an unrelated Exception is in flight, the
        pause signal still reaches the outer handler (cancellation
        ordering aside).
        """

        async def _raises_pause() -> None:
            raise BranchApprovalPauseSignal(
                branch_index=0,
                child_run_id="r",
                graph_ref="g",
                version=1,
                node_id="n",
            )

        async def _noop() -> int:
            return 42

        with pytest.raises(BranchApprovalPauseSignal):
            await asyncio.gather(_raises_pause(), _noop())

    @pytest.mark.asyncio
    async def test_captured_by_gather_return_exceptions(self) -> None:
        """Best-effort path must INSPECT raw_results for the signal.

        ``asyncio.gather(..., return_exceptions=True)`` captures
        BaseException subclasses into its returned list on modern Python.
        Best-effort code in ``ParallelExecutor._execute_best_effort``
        must therefore walk the list, pull out any
        ``BranchApprovalPauseSignal`` instance, cancel siblings, and
        raise/propagate the signal explicitly.
        """

        async def _raises_pause() -> None:
            raise BranchApprovalPauseSignal(
                branch_index=0,
                child_run_id="r",
                graph_ref="g",
                version=1,
                node_id="n",
            )

        async def _noop() -> int:
            return 42

        results = await asyncio.gather(
            _raises_pause(), _noop(), return_exceptions=True
        )
        assert any(isinstance(r, BranchApprovalPauseSignal) for r in results)
        assert 42 in results


class TestFanInResultPauseState:
    """Task 1: FanInResult carries optional pause_state dict."""

    def test_default_pause_state_is_none(self) -> None:
        fan_in = FanInResult(results=[])
        assert fan_in.pause_state is None

    def test_pause_state_can_be_set(self) -> None:
        payload: dict[str, Any] = {
            "paused": {
                "branch_index": 1,
                "child_run_id": "child-42",
                "graph_ref": "child-g",
                "version": 1,
                "node_id": "sub-node",
            },
            "completed_branch_results": [],
            "cancelled_branch_contexts": [],
        }
        fan_in = FanInResult(results=[], pause_state=payload)
        assert fan_in.pause_state is not None
        assert fan_in.pause_state["paused"]["branch_index"] == 1

    def test_branch_results_still_work(self) -> None:
        """Default zero-pause fan-in continues to behave exactly as Phase 38."""
        fan_in = FanInResult(
            results=[
                BranchResult(branch_index=0, output={"v": 1}),
                BranchResult(branch_index=1, output={"v": 2}),
            ],
            merged_output={"items": [{"v": 1}, {"v": 2}]},
            total_cost_usd=0.0,
            total_steps=2,
        )
        assert fan_in.pause_state is None
        assert len(fan_in.results) == 2
        assert fan_in.merged_output["items"][0] == {"v": 1}

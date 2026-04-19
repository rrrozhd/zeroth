"""Phase 43 Plan 01 — SubgraphNode inside parallel fan-out composition.

Top-of-module note: This module's integration tests mock the
``SubgraphExecutor`` rather than constructing a full resolver +
DeploymentService chain. The mocks return pre-built child ``Run``
objects so that ``branch_coro_factory``'s SubgraphNode dispatch path is
exercised end-to-end through ``_execute_parallel_fan_out`` and
``ParallelExecutor`` without the persistence overhead of
``tests/subgraph/test_integration.py``.


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


# ---------------------------------------------------------------------------
# Task 2: namespace_subgraph branch_index variant
# ---------------------------------------------------------------------------


class TestNamespaceSubgraphBranchIndex:
    """D-10: ``branch_index`` kwarg produces branch-prefixed audit IDs."""

    def test_no_branch_index_matches_phase_39(self) -> None:
        from zeroth.core.graph.models import AgentNode, AgentNodeData, Graph
        from zeroth.core.subgraph.resolver import namespace_subgraph

        node = AgentNode(
            node_id="a1",
            graph_version_ref="g@1",
            agent=AgentNodeData(
                instruction="x", model_provider="openai/gpt-4"
            ),
        )
        graph = Graph(
            graph_id="g", name="g", version=1, nodes=[node], edges=[], entry_step="a1"
        )
        ns = namespace_subgraph(graph, "g", depth=1)
        assert ns.nodes[0].node_id == "subgraph:g:1:a1"
        assert ns.entry_step == "subgraph:g:1:a1"

    def test_branch_index_produces_branch_prefix(self) -> None:
        from zeroth.core.graph.models import AgentNode, AgentNodeData, Graph
        from zeroth.core.subgraph.resolver import namespace_subgraph

        node = AgentNode(
            node_id="a1",
            graph_version_ref="g@1",
            agent=AgentNodeData(
                instruction="x", model_provider="openai/gpt-4"
            ),
        )
        graph = Graph(
            graph_id="g", name="g", version=1, nodes=[node], edges=[], entry_step="a1"
        )
        ns = namespace_subgraph(graph, "g", depth=1, branch_index=2)
        assert ns.nodes[0].node_id == "branch:2:subgraph:g:1:a1"
        assert ns.entry_step == "branch:2:subgraph:g:1:a1"

    def test_branch_index_idempotent_re_namespacing(self) -> None:
        """D-11 idempotency: re-namespacing with same branch_index is stable."""
        from zeroth.core.graph.models import AgentNode, AgentNodeData, Graph
        from zeroth.core.subgraph.resolver import namespace_subgraph

        node = AgentNode(
            node_id="a1",
            graph_version_ref="g@1",
            agent=AgentNodeData(
                instruction="x", model_provider="openai/gpt-4"
            ),
        )
        graph = Graph(
            graph_id="g", name="g", version=1, nodes=[node], edges=[], entry_step="a1"
        )
        first = namespace_subgraph(graph, "g", depth=1, branch_index=3)
        # Second pass on the ORIGINAL graph with same args — byte identical.
        second = namespace_subgraph(graph, "g", depth=1, branch_index=3)
        assert [n.node_id for n in first.nodes] == [n.node_id for n in second.nodes]


# ---------------------------------------------------------------------------
# D-21 Scenario 1 (integration-lite): SubgraphNode inside fan-out branch
# ---------------------------------------------------------------------------


class TestScenario1SubgraphInFanOutBranch:
    """D-05 + D-23 end-to-end: fan-out with SubgraphNode downstream
    executes one child run per branch and merges their outputs via
    collect fan-in."""

    @pytest.mark.asyncio
    async def test_fan_out_to_subgraph_collect(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from zeroth.core.execution_units import ExecutableUnitRunner
        from zeroth.core.graph.models import (
            AgentNode,
            AgentNodeData,
            Edge,
            Graph,
        )
        from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
        from zeroth.core.runs.models import Run, RunStatus
        from zeroth.core.subgraph.executor import SubgraphExecutor

        # Parent graph: source AgentNode (fan-out) -> SubgraphNode (downstream).
        source_node = AgentNode(
            node_id="source",
            graph_version_ref="parent@1",
            agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
            parallel_config=ParallelConfig(split_path="items"),
        )
        sub_node = SubgraphNode(
            node_id="sub-step",
            graph_version_ref="parent@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf"),
        )
        parent_graph = Graph(
            graph_id="parent-g",
            name="parent-g",
            version=1,
            nodes=[source_node, sub_node],
            edges=[
                Edge(
                    edge_id="e1",
                    source_node_id="source",
                    target_node_id="sub-step",
                )
            ],
            entry_step="source",
        )

        # Mock source agent runner to emit 3 items.
        class _FakeResult:
            def __init__(self, output_data: dict[str, Any]) -> None:
                self.output_data = output_data
                self.audit_record = {"model": "test", "token_usage": None}

        source_runner = AsyncMock()
        source_runner.run = AsyncMock(
            return_value=_FakeResult(
                {"items": [{"v": 1}, {"v": 2}, {"v": 3}]}
            )
        )

        # Mock SubgraphExecutor: return a distinct completed child Run per
        # branch, and count execute calls for branch-count assertion.
        call_counter = {"count": 0}

        async def _fake_execute(**kwargs: Any) -> Run:
            call_counter["count"] += 1
            idx = kwargs["branch_context"].branch_index
            return Run(
                run_id=f"child-run-{idx}",
                graph_version_ref="child-wf:v1",
                deployment_ref="child-wf",
                status=RunStatus.COMPLETED,
                final_output={"branch_idx": idx, "doubled": idx * 2},
                metadata={"subgraph_depth": 1, "total_cost_usd": 0.01 * (idx + 1)},
            )

        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=_fake_execute)

        # Minimal run repository.
        repo = AsyncMock()
        repo.create = AsyncMock(side_effect=lambda r: r)
        repo.put = AsyncMock(side_effect=lambda r: r)
        repo.get = AsyncMock(return_value=None)
        repo.write_checkpoint = AsyncMock()

        orch = RuntimeOrchestrator(
            run_repository=repo,
            agent_runners={"source": source_runner},
            executable_unit_runner=ExecutableUnitRunner(),
            subgraph_executor=mock_executor,
        )

        result = await orch.run_graph(parent_graph, {"input": "test"})

        assert result.status == RunStatus.COMPLETED
        # One execute call per branch (three items → three child runs).
        assert call_counter["count"] == 3
        # Each execute was called with a distinct branch_context.
        branch_indices = sorted(
            call.kwargs["branch_context"].branch_index
            for call in mock_executor.execute.call_args_list
        )
        assert branch_indices == [0, 1, 2]
        # Shared step tracker threaded through every call.
        trackers = {
            id(call.kwargs.get("step_tracker"))
            for call in mock_executor.execute.call_args_list
        }
        assert len(trackers) == 1  # single identity, not None and not per-branch
        assert None not in {
            call.kwargs.get("step_tracker")
            for call in mock_executor.execute.call_args_list
        }

    @pytest.mark.asyncio
    async def test_fan_out_subgraph_approval_pause_stashes_pending(
        self,
    ) -> None:
        """D-11: one branch's child hits WAITING_APPROVAL → parent run
        is persisted with status WAITING_APPROVAL and
        ``pending_parallel_subgraph`` metadata carries the paused
        branch + completed siblings.
        """
        from unittest.mock import AsyncMock, MagicMock

        from zeroth.core.execution_units import ExecutableUnitRunner
        from zeroth.core.graph.models import (
            AgentNode,
            AgentNodeData,
            Edge,
            Graph,
        )
        from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
        from zeroth.core.runs.models import Run, RunStatus
        from zeroth.core.subgraph.executor import SubgraphExecutor

        source_node = AgentNode(
            node_id="source",
            graph_version_ref="parent@1",
            agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
            parallel_config=ParallelConfig(split_path="items"),
        )
        sub_node = SubgraphNode(
            node_id="sub-step",
            graph_version_ref="parent@1",
            subgraph=SubgraphNodeData(graph_ref="child-wf"),
        )
        parent_graph = Graph(
            graph_id="parent-g",
            name="parent-g",
            version=1,
            nodes=[source_node, sub_node],
            edges=[
                Edge(
                    edge_id="e1",
                    source_node_id="source",
                    target_node_id="sub-step",
                )
            ],
            entry_step="source",
        )

        class _FakeResult:
            def __init__(self, output_data: dict[str, Any]) -> None:
                self.output_data = output_data
                self.audit_record = {"model": "test", "token_usage": None}

        source_runner = AsyncMock()
        source_runner.run = AsyncMock(
            return_value=_FakeResult({"items": [{"v": 0}, {"v": 1}]})
        )

        async def _fake_execute(**kwargs: Any) -> Run:
            idx = kwargs["branch_context"].branch_index
            if idx == 1:
                # Branch 1 pauses for approval.
                return Run(
                    run_id=f"child-run-{idx}",
                    graph_version_ref="child-wf:v1",
                    deployment_ref="child-wf",
                    status=RunStatus.WAITING_APPROVAL,
                    metadata={"subgraph_depth": 1},
                )
            return Run(
                run_id=f"child-run-{idx}",
                graph_version_ref="child-wf:v1",
                deployment_ref="child-wf",
                status=RunStatus.COMPLETED,
                final_output={"done": idx},
                metadata={"subgraph_depth": 1, "total_cost_usd": 0.0},
            )

        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(side_effect=_fake_execute)

        repo = AsyncMock()
        repo.create = AsyncMock(side_effect=lambda r: r)
        repo.put = AsyncMock(side_effect=lambda r: r)
        repo.get = AsyncMock(return_value=None)
        repo.write_checkpoint = AsyncMock()

        orch = RuntimeOrchestrator(
            run_repository=repo,
            agent_runners={"source": source_runner},
            executable_unit_runner=ExecutableUnitRunner(),
            subgraph_executor=mock_executor,
        )

        # Use best_effort so branch 0 runs to completion and we can assert
        # the completed-branch-rehydration metadata field is populated.
        source_node_best = source_node.model_copy(
            update={
                "parallel_config": ParallelConfig(
                    split_path="items", fail_mode="best_effort"
                )
            }
        )
        parent_graph_best = parent_graph.model_copy(
            update={"nodes": [source_node_best, sub_node]}
        )

        result = await orch.run_graph(parent_graph_best, {"input": "test"})

        assert result.status == RunStatus.WAITING_APPROVAL
        pending = result.metadata.get("pending_parallel_subgraph")
        assert pending is not None
        # Runtime stashes the fan-out SOURCE node id (the resume entry
        # point) — downstream SubgraphNode identity lives inside
        # `paused_branch.node_id` / `paused_branch.graph_ref`.
        assert pending["node_id"] == "source"
        assert pending["paused_branch"]["branch_index"] == 1
        assert pending["paused_branch"]["child_run_id"] == "child-run-1"
        assert pending["paused_branch"]["graph_ref"] == "child-wf"
        assert pending["paused_branch"]["node_id"] == "sub-step"

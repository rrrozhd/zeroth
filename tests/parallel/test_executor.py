"""Tests for zeroth.core.parallel.executor — ParallelExecutor fan-out/fan-in logic."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from zeroth.core.parallel.errors import (
    FanOutValidationError,
    ParallelExecutionError,
)
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import (
    BranchContext,
    BranchResult,
    ParallelConfig,
)


@pytest.fixture
def executor() -> ParallelExecutor:
    return ParallelExecutor()


@pytest.fixture
def basic_config() -> ParallelConfig:
    return ParallelConfig(split_path="items")


# ---------------------------------------------------------------------------
# split_fan_out
# ---------------------------------------------------------------------------


class TestSplitFanOut:
    """Tests for ParallelExecutor.split_fan_out()."""

    def test_valid_split_produces_branch_contexts(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        output_data = {"items": [{"a": 1}, {"b": 2}, {"c": 3}]}
        node = MagicMock()
        node.node_type = "agent"

        branches = executor.split_fan_out("run1", output_data, basic_config, node)

        assert len(branches) == 3
        assert branches[0].branch_index == 0
        assert branches[0].branch_id == "run1:branch:0"
        assert branches[0].input_payload == {"a": 1}
        assert branches[1].branch_index == 1
        assert branches[1].branch_id == "run1:branch:1"
        assert branches[1].input_payload == {"b": 2}
        assert branches[2].branch_index == 2
        assert branches[2].branch_id == "run1:branch:2"
        assert branches[2].input_payload == {"c": 3}

    def test_split_path_not_found_raises(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        node = MagicMock()
        node.node_type = "agent"

        with pytest.raises(FanOutValidationError, match="not found"):
            executor.split_fan_out("run1", {"other": "data"}, basic_config, node)

    def test_value_not_a_list_raises(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        node = MagicMock()
        node.node_type = "agent"

        with pytest.raises(FanOutValidationError, match="not a list"):
            executor.split_fan_out("run1", {"items": "not-a-list"}, basic_config, node)

    def test_empty_list_raises(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        node = MagicMock()
        node.node_type = "agent"

        with pytest.raises(FanOutValidationError, match="empty list"):
            executor.split_fan_out("run1", {"items": []}, basic_config, node)

    def test_exceeds_max_branches_raises(
        self, executor: ParallelExecutor
    ) -> None:
        config = ParallelConfig(split_path="items", max_branches=2)
        node = MagicMock()
        node.node_type = "agent"

        with pytest.raises(FanOutValidationError, match="exceeds max_branches"):
            executor.split_fan_out(
                "run1", {"items": [1, 2, 3]}, config, node
            )

    def test_nested_split_path(
        self, executor: ParallelExecutor
    ) -> None:
        config = ParallelConfig(split_path="data.results")
        node = MagicMock()
        node.node_type = "agent"
        output_data = {"data": {"results": [{"x": 1}, {"x": 2}]}}

        branches = executor.split_fan_out("run1", output_data, config, node)
        assert len(branches) == 2

    def test_non_dict_items_wrapped(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        """Non-dict items are wrapped as {"_item": value}."""
        node = MagicMock()
        node.node_type = "agent"
        output_data = {"items": [1, 2, 3]}

        branches = executor.split_fan_out("run1", output_data, basic_config, node)
        assert branches[0].input_payload == {"_item": 1}
        assert branches[1].input_payload == {"_item": 2}

    def test_human_approval_node_rejected(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        """HumanApprovalNode cannot be used with parallel fan-out."""
        node = MagicMock()
        node.node_type = "human_approval"

        with pytest.raises(FanOutValidationError, match="HumanApprovalNode"):
            executor.split_fan_out(
                "run1", {"items": [1, 2]}, basic_config, node
            )


# ---------------------------------------------------------------------------
# execute_branches
# ---------------------------------------------------------------------------


class TestExecuteBranches:
    """Tests for ParallelExecutor.execute_branches()."""

    @pytest.mark.asyncio
    async def test_best_effort_mixed_results(
        self, executor: ParallelExecutor
    ) -> None:
        """2 succeed, 1 fails in best-effort mode."""
        config = ParallelConfig(split_path="items", fail_mode="best_effort")

        contexts = [
            BranchContext(branch_index=0, branch_id="r:branch:0", input_payload={"v": 1}),
            BranchContext(branch_index=1, branch_id="r:branch:1", input_payload={"v": 2}),
            BranchContext(branch_index=2, branch_id="r:branch:2", input_payload={"v": 3}),
        ]

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 1:
                raise ValueError("branch 1 failed")
            return {"result": ctx.input_payload["v"] * 10}

        results = await executor.execute_branches(contexts, branch_coro, config)

        assert len(results) == 3
        assert results[0].output == {"result": 10}
        assert results[0].error is None
        assert results[1].output is None
        assert results[1].error is not None
        assert "branch 1 failed" in results[1].error
        assert results[2].output == {"result": 30}
        assert results[2].error is None

    @pytest.mark.asyncio
    async def test_fail_fast_cancels_remaining(
        self, executor: ParallelExecutor
    ) -> None:
        """Fail-fast should cancel remaining tasks on first failure."""
        config = ParallelConfig(split_path="items", fail_mode="fail_fast")

        contexts = [
            BranchContext(branch_index=0, branch_id="r:branch:0", input_payload={}),
            BranchContext(branch_index=1, branch_id="r:branch:1", input_payload={}),
            BranchContext(branch_index=2, branch_id="r:branch:2", input_payload={}),
        ]

        call_tracker: list[int] = []

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 0:
                raise RuntimeError("immediate failure")
            # Other branches should be cancelled before completing
            await asyncio.sleep(10)
            call_tracker.append(ctx.branch_index)
            return {"done": True}

        with pytest.raises(ParallelExecutionError):
            await executor.execute_branches(contexts, branch_coro, config)

        # Other branches should not have completed
        assert len(call_tracker) == 0

    @pytest.mark.asyncio
    async def test_best_effort_all_succeed(
        self, executor: ParallelExecutor
    ) -> None:
        """All branches succeed in best-effort mode."""
        config = ParallelConfig(split_path="items", fail_mode="best_effort")

        contexts = [
            BranchContext(branch_index=i, branch_id=f"r:branch:{i}", input_payload={"i": i})
            for i in range(3)
        ]

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            return {"val": ctx.branch_index}

        results = await executor.execute_branches(contexts, branch_coro, config)
        assert all(r.error is None for r in results)
        assert [r.output for r in results] == [{"val": 0}, {"val": 1}, {"val": 2}]


# ---------------------------------------------------------------------------
# collect_fan_in
# ---------------------------------------------------------------------------


class TestCollectFanIn:
    """Tests for ParallelExecutor.collect_fan_in()."""

    def test_all_successful(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        branch_results = [
            BranchResult(branch_index=0, output={"a": 1}),
            BranchResult(branch_index=1, output={"b": 2}),
            BranchResult(branch_index=2, output={"c": 3}),
        ]
        base_output = {"other": "data"}

        fan_in = executor.collect_fan_in(branch_results, basic_config, base_output)

        assert len(fan_in.results) == 3
        # Outputs ordered by branch_index
        assert fan_in.merged_output["items"] == [{"a": 1}, {"b": 2}, {"c": 3}]
        assert fan_in.merged_output["other"] == "data"

    def test_failed_branch_produces_none(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        """Failed branches produce None in the output list (D-08)."""
        branch_results = [
            BranchResult(branch_index=0, output={"a": 1}),
            BranchResult(branch_index=1, output=None, error="failed"),
            BranchResult(branch_index=2, output={"c": 3}),
        ]

        fan_in = executor.collect_fan_in(branch_results, basic_config, {})

        assert fan_in.merged_output["items"] == [{"a": 1}, None, {"c": 3}]

    def test_merge_path_defaults_to_split_path(
        self, executor: ParallelExecutor
    ) -> None:
        """When no merge_path override, it defaults to split_path."""
        config = ParallelConfig(split_path="data.results")
        branch_results = [
            BranchResult(branch_index=0, output={"x": 1}),
        ]

        fan_in = executor.collect_fan_in(branch_results, config, {})
        # Should be set at "data.results" path
        assert fan_in.merged_output["data"]["results"] == [{"x": 1}]

    def test_cost_and_steps_aggregation(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        branch_results = [
            BranchResult(
                branch_index=0,
                output={"a": 1},
                cost_usd=0.5,
                execution_history=[{"step": 1}, {"step": 2}],
            ),
            BranchResult(
                branch_index=1,
                output={"b": 2},
                cost_usd=0.3,
                execution_history=[{"step": 1}],
            ),
        ]

        fan_in = executor.collect_fan_in(branch_results, basic_config, {})
        assert fan_in.total_cost_usd == pytest.approx(0.8)
        assert fan_in.total_steps == 3

    def test_ordering_preserved_regardless_of_input_order(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        """Results should be ordered by branch_index even if input is shuffled."""
        branch_results = [
            BranchResult(branch_index=2, output={"c": 3}),
            BranchResult(branch_index=0, output={"a": 1}),
            BranchResult(branch_index=1, output={"b": 2}),
        ]

        fan_in = executor.collect_fan_in(branch_results, basic_config, {})
        assert fan_in.merged_output["items"] == [{"a": 1}, {"b": 2}, {"c": 3}]


# ---------------------------------------------------------------------------
# Node type validation
# ---------------------------------------------------------------------------


class TestNodeTypeValidation:
    """Tests for fan-out node type checks."""

    def test_human_approval_node_with_parallel_config_raises(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        """HumanApprovalNode should be rejected at fan-out validation time."""
        from zeroth.core.graph.models import HumanApprovalNode, HumanApprovalNodeData

        node = HumanApprovalNode(
            node_id="approval1",
            graph_version_ref="gv1",
            human_approval=HumanApprovalNodeData(),
        )

        with pytest.raises(FanOutValidationError, match="HumanApprovalNode"):
            executor.split_fan_out("run1", {"items": [1, 2]}, basic_config, node)

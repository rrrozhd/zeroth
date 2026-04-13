"""Tests for zeroth.core.parallel.models — ParallelConfig, BranchContext, BranchResult, FanInResult, GlobalStepTracker."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from zeroth.core.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    GlobalStepTracker,
    ParallelConfig,
)
from zeroth.core.parallel.errors import ParallelStepLimitError


# ---------------------------------------------------------------------------
# ParallelConfig
# ---------------------------------------------------------------------------


class TestParallelConfig:
    """Tests for ParallelConfig Pydantic model."""

    def test_minimal_construction(self) -> None:
        cfg = ParallelConfig(split_path="items")
        assert cfg.split_path == "items"
        assert cfg.merge_strategy == "collect"
        assert cfg.fail_mode == "fail_fast"
        assert cfg.max_branches is None

    def test_all_fields(self) -> None:
        cfg = ParallelConfig(
            split_path="data.results",
            merge_strategy="reduce",
            fail_mode="best_effort",
            max_branches=5,
        )
        assert cfg.split_path == "data.results"
        assert cfg.merge_strategy == "reduce"
        assert cfg.fail_mode == "best_effort"
        assert cfg.max_branches == 5

    def test_invalid_merge_strategy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="x", merge_strategy="invalid")

    def test_invalid_fail_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="x", fail_mode="invalid")

    def test_max_branches_ge_1(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="x", max_branches=0)

    def test_max_branches_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="x", max_branches=-1)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="x", unknown_field="bad")


# ---------------------------------------------------------------------------
# BranchContext
# ---------------------------------------------------------------------------


class TestBranchContext:
    """Tests for BranchContext dataclass."""

    def test_construction_with_all_fields(self) -> None:
        ctx = BranchContext(
            branch_index=0,
            branch_id="run1:branch:0",
            input_payload={"key": "value"},
        )
        assert ctx.branch_index == 0
        assert ctx.branch_id == "run1:branch:0"
        assert ctx.input_payload == {"key": "value"}

    def test_isolated_defaults(self) -> None:
        """Each BranchContext starts with empty isolated state (D-05)."""
        ctx = BranchContext(
            branch_index=1,
            branch_id="run1:branch:1",
            input_payload={},
        )
        assert ctx.node_visit_counts == {}
        assert ctx.execution_history == []
        assert ctx.audit_refs == []
        assert ctx.condition_results == []
        assert ctx.metadata == {}

    def test_mutable_defaults_not_shared(self) -> None:
        """Two BranchContexts should not share mutable default instances."""
        ctx1 = BranchContext(branch_index=0, branch_id="r:branch:0", input_payload={})
        ctx2 = BranchContext(branch_index=1, branch_id="r:branch:1", input_payload={})
        ctx1.node_visit_counts["a"] = 1
        assert "a" not in ctx2.node_visit_counts


# ---------------------------------------------------------------------------
# BranchResult
# ---------------------------------------------------------------------------


class TestBranchResult:
    """Tests for BranchResult dataclass."""

    def test_success_result(self) -> None:
        r = BranchResult(branch_index=0, output={"answer": 42})
        assert r.branch_index == 0
        assert r.output == {"answer": 42}
        assert r.error is None
        assert r.audit_refs == []
        assert r.execution_history == []
        assert r.cost_usd == 0.0

    def test_failure_result(self) -> None:
        r = BranchResult(branch_index=1, output=None, error="boom")
        assert r.output is None
        assert r.error == "boom"


# ---------------------------------------------------------------------------
# FanInResult
# ---------------------------------------------------------------------------


class TestFanInResult:
    """Tests for FanInResult dataclass."""

    def test_construction(self) -> None:
        br = BranchResult(branch_index=0, output={"x": 1})
        fin = FanInResult(results=[br], total_cost_usd=0.5, total_steps=3)
        assert len(fin.results) == 1
        assert fin.total_cost_usd == 0.5
        assert fin.total_steps == 3

    def test_defaults(self) -> None:
        fin = FanInResult(results=[])
        assert fin.merged_output == {}
        assert fin.total_cost_usd == 0.0
        assert fin.total_steps == 0


# ---------------------------------------------------------------------------
# GlobalStepTracker
# ---------------------------------------------------------------------------


class TestGlobalStepTracker:
    """Tests for GlobalStepTracker async step limiter."""

    @pytest.mark.asyncio
    async def test_increment_within_limit(self) -> None:
        tracker = GlobalStepTracker(current_steps=0, max_steps=5)
        await tracker.increment()
        assert tracker.count == 1

    @pytest.mark.asyncio
    async def test_increment_raises_at_limit(self) -> None:
        tracker = GlobalStepTracker(current_steps=4, max_steps=5)
        with pytest.raises(ParallelStepLimitError):
            await tracker.increment()

    @pytest.mark.asyncio
    async def test_increment_raises_above_limit(self) -> None:
        tracker = GlobalStepTracker(current_steps=0, max_steps=3)
        await tracker.increment()
        await tracker.increment()
        await tracker.increment()
        with pytest.raises(ParallelStepLimitError):
            await tracker.increment()

    @pytest.mark.asyncio
    async def test_concurrent_increments_respect_limit(self) -> None:
        """Spawn 10 concurrent increments with max=5. Exactly 5 should succeed."""
        tracker = GlobalStepTracker(current_steps=0, max_steps=5)
        results: list[bool] = []

        async def try_increment() -> bool:
            try:
                await tracker.increment()
                return True
            except ParallelStepLimitError:
                return False

        results = await asyncio.gather(*[try_increment() for _ in range(10)])
        assert sum(results) == 5
        assert tracker.count == 5


# ---------------------------------------------------------------------------
# NodeBase with parallel_config
# ---------------------------------------------------------------------------


class TestNodeBaseParallelConfig:
    """Tests for parallel_config field on NodeBase via AgentNode."""

    def test_agent_node_with_parallel_config(self) -> None:
        from zeroth.core.graph.models import AgentNode, AgentNodeData

        config = ParallelConfig(split_path="items", max_branches=3)
        node = AgentNode(
            node_id="n1",
            graph_version_ref="gv1",
            agent=AgentNodeData(instruction="do stuff", model_provider="openai"),
            parallel_config=config,
        )
        assert node.parallel_config is not None
        assert node.parallel_config.split_path == "items"
        assert node.parallel_config.max_branches == 3

    def test_agent_node_without_parallel_config(self) -> None:
        from zeroth.core.graph.models import AgentNode, AgentNodeData

        node = AgentNode(
            node_id="n2",
            graph_version_ref="gv1",
            agent=AgentNodeData(instruction="do stuff", model_provider="openai"),
        )
        assert node.parallel_config is None

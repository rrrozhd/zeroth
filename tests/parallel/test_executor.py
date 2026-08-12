"""Tests for zeroth.runtime.parallel.executor — ParallelExecutor fan-out/fan-in logic."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.parallel.errors import (
    BranchApprovalPauseSignal,
    FanOutValidationError,
    MultipleBranchPauseError,
    ParallelExecutionError,
)
from zeroth.runtime.parallel.executor import ParallelExecutor
from zeroth.runtime.parallel.models import (
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

    def test_exceeds_max_branches_raises(self, executor: ParallelExecutor) -> None:
        config = ParallelConfig(split_path="items", max_branches=2)
        node = MagicMock()
        node.node_type = "agent"

        with pytest.raises(FanOutValidationError, match="exceeds max_branches"):
            executor.split_fan_out("run1", {"items": [1, 2, 3]}, config, node)

    def test_nested_split_path(self, executor: ParallelExecutor) -> None:
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
            executor.split_fan_out("run1", {"items": [1, 2]}, basic_config, node)


# ---------------------------------------------------------------------------
# execute_branches
# ---------------------------------------------------------------------------


class TestExecuteBranches:
    """Tests for ParallelExecutor.execute_branches()."""

    @pytest.mark.asyncio
    async def test_best_effort_mixed_results(self, executor: ParallelExecutor) -> None:
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
    async def test_fail_fast_cancels_remaining(self, executor: ParallelExecutor) -> None:
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
    async def test_best_effort_all_succeed(self, executor: ParallelExecutor) -> None:
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

    @pytest.mark.asyncio
    async def test_best_effort_multiple_pauses_fail_loud(self, executor: ParallelExecutor) -> None:
        # B10: best_effort runs every branch to completion, so >1 branch can hit
        # an approval gate. The old code kept only the LAST pause signal, orphaning
        # the earlier paused branch's child run. It must now fail loudly.
        config = ParallelConfig(split_path="items", fail_mode="best_effort")
        contexts = [
            BranchContext(branch_index=i, branch_id=f"r:branch:{i}", input_payload={})
            for i in range(3)
        ]

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index in (0, 2):
                raise BranchApprovalPauseSignal(
                    branch_index=ctx.branch_index,
                    child_run_id=f"child-{ctx.branch_index}",
                    graph_ref="child-wf",
                    version=1,
                    node_id="sub",
                )
            return {"ok": ctx.branch_index}

        with pytest.raises(MultipleBranchPauseError, match="2 branches paused"):
            await executor.execute_branches(contexts, branch_coro, config)

    @pytest.mark.asyncio
    async def test_fail_fast_multiple_simultaneous_pauses_fail_loud(
        self, executor: ParallelExecutor
    ) -> None:
        config = ParallelConfig(split_path="items", fail_mode="fail_fast")
        contexts = [
            BranchContext(branch_index=i, branch_id=f"r:branch:{i}", input_payload={})
            for i in range(2)
        ]
        release = asyncio.Event()
        arrived = 0

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            nonlocal arrived
            arrived += 1
            if arrived == len(contexts):
                release.set()
            await release.wait()
            raise BranchApprovalPauseSignal(
                branch_index=ctx.branch_index,
                child_run_id=f"child-{ctx.branch_index}",
                graph_ref="child-wf",
                version=1,
                node_id="sub",
            )

        with pytest.raises(MultipleBranchPauseError, match="2 branches paused"):
            await executor.execute_branches(contexts, branch_coro, config)

    @pytest.mark.asyncio
    async def test_best_effort_single_pause_still_propagates(
        self, executor: ParallelExecutor
    ) -> None:
        # A single pause is the supported case: re-raised as BranchApprovalPauseSignal
        # with the completed sibling attached (unchanged behavior).
        config = ParallelConfig(split_path="items", fail_mode="best_effort")
        contexts = [
            BranchContext(branch_index=i, branch_id=f"r:branch:{i}", input_payload={})
            for i in range(2)
        ]

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 1:
                raise BranchApprovalPauseSignal(
                    branch_index=1,
                    child_run_id="child-1",
                    graph_ref="child-wf",
                    version=1,
                    node_id="sub",
                )
            return {"ok": 0}

        with pytest.raises(BranchApprovalPauseSignal) as exc_info:
            await executor.execute_branches(contexts, branch_coro, config)
        assert exc_info.value.branch_index == 1
        completed = getattr(exc_info.value, "completed_branch_results", [])
        assert [br.branch_index for br in completed] == [0]


# ---------------------------------------------------------------------------
# Concurrency controls: max_concurrency / batch_size / branch_timeout_seconds
# ---------------------------------------------------------------------------


def _contexts(n: int) -> list[BranchContext]:
    return [
        BranchContext(branch_index=i, branch_id=f"r:branch:{i}", input_payload={"i": i})
        for i in range(n)
    ]


class TestConcurrencyControls:
    """Batch-parallelized fan-out: worker cap, sequential waves, per-branch timeout."""

    @pytest.mark.asyncio
    async def test_max_concurrency_bounds_simultaneous_branches(
        self, executor: ParallelExecutor
    ) -> None:
        """No more than ``max_concurrency`` branches run at once; all still complete."""
        config = ParallelConfig(split_path="items", max_concurrency=2)
        live = 0
        peak = 0

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
            live -= 1
            return {"i": ctx.branch_index}

        results = await executor.execute_branches(_contexts(6), branch_coro, config)

        assert peak <= 2, peak
        assert peak == 2  # with 6 branches and a cap of 2, the cap is actually hit
        assert [r.output["i"] for r in results] == list(range(6))

    @pytest.mark.asyncio
    async def test_unbounded_concurrency_runs_all_at_once(self, executor: ParallelExecutor) -> None:
        """Without a cap, every branch is in flight simultaneously (historical default)."""
        config = ParallelConfig(split_path="items")  # no max_concurrency
        live = 0
        peak = 0

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return {"i": ctx.branch_index}

        await executor.execute_branches(_contexts(5), branch_coro, config)
        assert peak == 5

    @pytest.mark.asyncio
    async def test_batch_size_runs_sequential_waves(self, executor: ParallelExecutor) -> None:
        """Wave N+1 starts only after wave N fully completes (a barrier)."""
        config = ParallelConfig(split_path="items", batch_size=2)
        events: list[tuple[str, int]] = []

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            events.append(("start", ctx.branch_index))
            await asyncio.sleep(0.01)
            events.append(("end", ctx.branch_index))
            return {"i": ctx.branch_index}

        results = await executor.execute_branches(_contexts(5), branch_coro, config)

        # All 5 processed, in order.
        assert [r.output["i"] for r in results] == list(range(5))
        # Waves are [0,1], [2,3], [4]. Every branch in an earlier wave must END
        # before any branch in a later wave STARTs.
        start_pos = {idx: pos for pos, (kind, idx) in enumerate(events) if kind == "start"}
        end_pos = {idx: pos for pos, (kind, idx) in enumerate(events) if kind == "end"}
        waves = [[0, 1], [2, 3], [4]]
        for earlier, later in zip(waves, waves[1:], strict=False):
            latest_end = max(end_pos[i] for i in earlier)
            earliest_start = min(start_pos[i] for i in later)
            assert latest_end < earliest_start, (earlier, later, events)

    @pytest.mark.asyncio
    async def test_batch_size_within_wave_is_concurrent(self, executor: ParallelExecutor) -> None:
        """Branches *within* a wave still run concurrently (not serialized)."""
        config = ParallelConfig(split_path="items", batch_size=3)
        live = 0
        peak = 0

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return {"i": ctx.branch_index}

        await executor.execute_branches(_contexts(3), branch_coro, config)
        assert peak == 3  # the whole wave overlaps

    @pytest.mark.asyncio
    async def test_branch_timeout_best_effort_becomes_error(
        self, executor: ParallelExecutor
    ) -> None:
        """A branch exceeding the timeout fails like any other branch (best_effort)."""
        config = ParallelConfig(
            split_path="items", fail_mode="best_effort", branch_timeout_seconds=0.05
        )

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 1:
                await asyncio.sleep(10)  # will time out
            return {"i": ctx.branch_index}

        results = await executor.execute_branches(_contexts(3), branch_coro, config)

        assert results[0].error is None and results[0].output == {"i": 0}
        assert results[1].output is None and results[1].error is not None  # timed out
        assert results[2].error is None and results[2].output == {"i": 2}

    @pytest.mark.asyncio
    async def test_branch_timeout_fail_fast_fails_the_fanout(
        self, executor: ParallelExecutor
    ) -> None:
        """Under fail_fast a timed-out branch cancels the fan-out."""
        config = ParallelConfig(
            split_path="items", fail_mode="fail_fast", branch_timeout_seconds=0.05
        )

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 0:
                await asyncio.sleep(10)
            await asyncio.sleep(10)
            return {"i": ctx.branch_index}

        with pytest.raises(ParallelExecutionError):
            await executor.execute_branches(_contexts(3), branch_coro, config)

    @pytest.mark.asyncio
    async def test_batched_approval_pause_fails_loud(self, executor: ParallelExecutor) -> None:
        """An approval pause inside a MULTI-wave fan-out is rejected loudly.

        Earlier waves already ran their side effects; batched resume can't
        represent partially-completed waves, so we fail rather than corrupt state.
        """
        config = ParallelConfig(split_path="items", batch_size=1)  # 3 waves of 1

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 1:  # pauses in the second wave
                raise BranchApprovalPauseSignal(
                    branch_index=1,
                    child_run_id="child-1",
                    graph_ref="child-wf",
                    version=1,
                    node_id="sub",
                )
            return {"i": ctx.branch_index}

        with pytest.raises(ParallelExecutionError, match="batched fan-out"):
            await executor.execute_branches(_contexts(3), branch_coro, config)

    @pytest.mark.asyncio
    async def test_single_wave_batch_still_propagates_pause(
        self, executor: ParallelExecutor
    ) -> None:
        """batch_size covering everything = one wave → pause propagates normally."""
        config = ParallelConfig(split_path="items", batch_size=10, fail_mode="best_effort")

        async def branch_coro(ctx: BranchContext) -> dict[str, Any]:
            if ctx.branch_index == 1:
                raise BranchApprovalPauseSignal(
                    branch_index=1,
                    child_run_id="child-1",
                    graph_ref="child-wf",
                    version=1,
                    node_id="sub",
                )
            return {"i": ctx.branch_index}

        with pytest.raises(BranchApprovalPauseSignal):
            await executor.execute_branches(_contexts(2), branch_coro, config)


# ---------------------------------------------------------------------------
# collect_fan_in
# ---------------------------------------------------------------------------


class TestCollectFanIn:
    """Tests for ParallelExecutor.collect_fan_in()."""

    def test_all_successful(self, executor: ParallelExecutor, basic_config: ParallelConfig) -> None:
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

    def test_merge_path_defaults_to_split_path(self, executor: ParallelExecutor) -> None:
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

    def test_cost_aggregation_keeps_estimates_out_of_recorded_total(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        fan_in = executor.collect_fan_in(
            [
                BranchResult(
                    branch_index=0,
                    output={"a": 1},
                    cost_usd=0.5,
                    cost_measurement=MeasurementState.MEASURED,
                ),
                BranchResult(
                    branch_index=1,
                    output={"b": 2},
                    estimated_cost_usd=0.3,
                    cost_measurement=MeasurementState.ESTIMATED,
                ),
            ],
            basic_config,
            {},
        )

        assert fan_in.total_cost_usd == 0.5
        assert fan_in.total_estimated_cost_usd == 0.3
        assert fan_in.cost_measurement is MeasurementState.ESTIMATED

    def test_cost_aggregation_preserves_unknown_branch(
        self, executor: ParallelExecutor, basic_config: ParallelConfig
    ) -> None:
        fan_in = executor.collect_fan_in(
            [
                BranchResult(
                    branch_index=0,
                    output={"a": 1},
                    cost_usd=0.5,
                    cost_measurement=MeasurementState.MEASURED,
                ),
                BranchResult(branch_index=1, output={"b": 2}),
            ],
            basic_config,
            {},
        )

        assert fan_in.total_cost_usd == 0.5
        assert fan_in.cost_measurement is MeasurementState.UNMEASURED

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
        from zeroth.contracts.graph.models import HumanApprovalNode, HumanApprovalNodeData

        node = HumanApprovalNode(
            node_id="approval1",
            graph_version_ref="gv1",
            human_approval=HumanApprovalNodeData(),
        )

        with pytest.raises(FanOutValidationError, match="HumanApprovalNode"):
            executor.split_fan_out("run1", {"items": [1, 2]}, basic_config, node)

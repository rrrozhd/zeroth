"""Tests for zeroth.runtime.parallel.models.

ParallelConfig, BranchContext, BranchResult, FanInResult and GlobalStepTracker.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.parallel.errors import FanOutValidationError, ParallelStepLimitError
from zeroth.runtime.parallel.executor import ParallelExecutor
from zeroth.runtime.parallel.models import (
    DEFAULT_MAX_BRANCHES,
    DEFAULT_MAX_CONCURRENCY,
    BranchContext,
    BranchResult,
    FanInResult,
    GlobalStepTracker,
    ParallelConfig,
)

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

    def test_an_absent_fan_out_bound_names_a_default_not_infinity(self) -> None:
        """ZER-48 / A06-15: ``None`` must mean "use the default", never "no limit".

        The fields stay optional because this model's constructor signature is
        pinned by the frozen protected-surface fixture. What changed is what
        ``None`` *means*: the executor resolves it to a real ceiling, because the
        branch list comes from the preceding node's output and an absent cap let
        the fan-out width be chosen by data.

        Asserted on the constants rather than on the field defaults, since the
        field defaults deliberately remain ``None``.
        """
        cfg = ParallelConfig(split_path="items")
        assert cfg.max_branches is None
        assert cfg.max_concurrency is None

        assert DEFAULT_MAX_BRANCHES > 0
        assert DEFAULT_MAX_CONCURRENCY > 0

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
        assert r.cost_usd is None
        assert r.cost_measurement is MeasurementState.UNMEASURED

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
        assert fin.total_cost_usd is None
        assert fin.total_estimated_cost_usd is None
        assert fin.cost_measurement is MeasurementState.UNMEASURED
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
        tracker = GlobalStepTracker(current_steps=5, max_steps=5)
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
        from zeroth.contracts.graph.models import AgentNode, AgentNodeData

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
        from zeroth.contracts.graph.models import AgentNode, AgentNodeData

        node = AgentNode(
            node_id="n2",
            graph_version_ref="gv1",
            agent=AgentNodeData(instruction="do stuff", model_provider="openai"),
        )
        assert node.parallel_config is None


# ---------------------------------------------------------------------------
# The default resolution, driven through the executor
# ---------------------------------------------------------------------------


class _PlainNode:
    """A fan-out target with no ``node_type``, so only the width rule applies."""


class TestAbsentBoundsResolveThroughTheExecutor:
    """ZER-48 / A06-15: ``None`` is resolved to a ceiling where it is *consumed*.

    The model keeps ``max_branches``/``max_concurrency`` optional -- its
    constructor signature is pinned by the frozen protected-surface fixture -- so
    asserting on the model can only observe that the fields are still ``None``.
    The property that actually changed lives in ``ParallelExecutor``: an absent
    cap resolves to the default rather than to no cap at all. These drive the
    executor, because reverting it leaves every model assertion green.
    """

    @staticmethod
    def _config(**overrides: object) -> ParallelConfig:
        return ParallelConfig(split_path="items", **overrides)  # type: ignore[arg-type]

    def test_a_fan_out_at_the_default_ceiling_is_allowed(self) -> None:
        """Exactly ``DEFAULT_MAX_BRANCHES`` items is the widest accepted fan-out."""
        payload = {"items": [{"n": index} for index in range(DEFAULT_MAX_BRANCHES)]}

        contexts = ParallelExecutor().split_fan_out("run1", payload, self._config(), _PlainNode())

        assert len(contexts) == DEFAULT_MAX_BRANCHES

    def test_a_fan_out_past_the_default_ceiling_is_refused(self) -> None:
        """One item past the default is rejected, so ``None`` is not "no limit".

        This is the measured defect: the branch list is the preceding node's
        output, so before the resolution a 50,000-element result produced 50,000
        branch contexts without complaint.
        """
        payload = {"items": [{"n": index} for index in range(DEFAULT_MAX_BRANCHES + 1)]}

        with pytest.raises(FanOutValidationError, match=str(DEFAULT_MAX_BRANCHES)):
            ParallelExecutor().split_fan_out("run1", payload, self._config(), _PlainNode())

    def test_an_explicit_branch_cap_still_wins_over_the_default(self) -> None:
        """A configured ceiling is honoured, not silently widened to the default."""
        payload = {"items": [{"n": index} for index in range(4)]}

        with pytest.raises(FanOutValidationError, match="max_branches 3"):
            ParallelExecutor().split_fan_out(
                "run1", payload, self._config(max_branches=3), _PlainNode()
            )

    @staticmethod
    async def _peak_in_flight(branches: int, config: ParallelConfig) -> tuple[int, int]:
        """Run ``branches`` overlapping branches; return (peak in flight, results)."""
        contexts = [
            BranchContext(branch_index=index, branch_id=f"r:branch:{index}", input_payload={})
            for index in range(branches)
        ]
        state = {"in_flight": 0, "peak": 0}

        async def factory(ctx: BranchContext) -> dict:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            # A real suspension point: without one the branches never overlap and
            # any ceiling would look respected. Long enough that every branch the
            # semaphore admits is in flight before the first one wakes.
            await asyncio.sleep(0.05)
            state["in_flight"] -= 1
            return {"index": ctx.branch_index}

        results = await ParallelExecutor().execute_branches(contexts, factory, config)
        return state["peak"], len(results)

    @pytest.mark.asyncio
    async def test_an_absent_throttle_caps_simultaneous_branches_at_the_default(self) -> None:
        """With no ``max_concurrency`` the peak in flight is the default, not the width.

        Asserted as equality rather than ``<=``: a factory that never suspends
        would satisfy ``<=`` with a peak of one, which is how a throttle test
        passes while no throttle exists.
        """
        branches = DEFAULT_MAX_CONCURRENCY * 2

        peak, completed = await self._peak_in_flight(branches, self._config())

        assert completed == branches
        assert peak == DEFAULT_MAX_CONCURRENCY

    @pytest.mark.asyncio
    async def test_an_explicit_throttle_still_wins_over_the_default(self) -> None:
        """A configured throttle is applied instead of the default."""
        branches = DEFAULT_MAX_CONCURRENCY * 2

        peak, completed = await self._peak_in_flight(branches, self._config(max_concurrency=4))

        assert completed == branches
        assert peak == 4

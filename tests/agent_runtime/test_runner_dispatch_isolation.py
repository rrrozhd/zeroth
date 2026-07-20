from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ToolAttachmentManifest,
)
from zeroth.runtime.context.models import ContextWindowSettings
from zeroth.runtime.context.strategies import LLMSummarizationStrategy
from zeroth.runtime.context.tracker import ContextWindowTracker


class _AgentInput(BaseModel):
    query: str


class _AgentOutput(BaseModel):
    answer: str


class _NestedMutableStrategy:
    def __init__(self) -> None:
        self.events: list[list[str]] = [["prototype"]]


class _UncopyableStrategy:
    def __deepcopy__(self, memo: dict[int, object]) -> _UncopyableStrategy:
        raise TypeError("strategy cannot be copied")


class _LegacyTracker:
    def __init__(self) -> None:
        self.events: list[list[str]] = [["prototype"]]

    async def maybe_compact(
        self,
        messages: list[object],
        model_name: str,
    ) -> tuple[list[object], None]:
        return messages, None


def test_runner_fork_rebuilds_dispatch_state_and_preserves_safe_dependencies() -> None:
    strategy_provider = object()
    tracker = ContextWindowTracker(
        settings=ContextWindowSettings(
            max_context_tokens=8_000,
            summary_trigger_ratio=0.75,
            preserve_recent_messages_count=6,
        ),
        strategy=LLMSummarizationStrategy(strategy_provider),
    )
    tracker._accumulated_tokens = 4_200
    tracker._compaction_count = 3
    tracker._last_strategy_name = "preconfigured"

    provider = DeterministicProviderAdapter([])
    memory_resolver = object()
    budget_enforcer = object()
    tool_executor = object()
    runner = AgentRunner(
        AgentConfig(
            name="dispatch-prototype",
            instruction="Return a valid answer.",
            model_name="governai:test",
            input_model=_AgentInput,
            output_model=_AgentOutput,
            tags=["prototype"],
            tool_attachments=[
                ToolAttachmentManifest(
                    alias="search",
                    executable_unit_ref="eu://search",
                )
            ],
        ),
        provider,
        tool_executor=tool_executor,
        granted_tool_permissions=["net:query"],
        memory_resolver=memory_resolver,
        budget_enforcer=budget_enforcer,
        context_tracker=tracker,
    )
    runner._mcp_manager = object()  # type: ignore[assignment]

    fork = runner.fork_for_dispatch()

    assert fork is not runner
    assert fork.config == runner.config
    assert fork.config is not runner.config
    fork.config.tags.append("dispatch")
    assert runner.config.tags == ["prototype"]

    assert fork.tool_bridge is not runner.tool_bridge
    assert fork.tool_bridge.registry.declared_aliases() == ["search"]
    assert fork._mcp_manager is None
    assert fork.granted_tool_permissions is not runner.granted_tool_permissions
    fork.granted_tool_permissions.append("fs:read")
    assert runner.granted_tool_permissions == ["net:query"]

    fork_tracker = fork.context_tracker
    assert fork_tracker is not tracker
    assert fork_tracker.settings == tracker.settings
    assert fork_tracker.settings is not tracker.settings
    fork_tracker.settings.max_context_tokens = 16_000
    assert tracker.settings.max_context_tokens == 8_000
    assert fork_tracker.strategy is not tracker.strategy
    assert fork_tracker.strategy._provider is strategy_provider
    assert fork_tracker.state.accumulated_tokens == 0
    assert fork_tracker.state.compaction_count == 0
    assert fork_tracker.state.last_compaction_strategy is None

    assert fork.provider is provider
    assert fork.memory_resolver is memory_resolver
    assert fork.budget_enforcer is budget_enforcer
    assert fork.tool_executor is tool_executor


def test_context_tracker_fork_deep_copies_custom_strategy_state() -> None:
    tracker = ContextWindowTracker(
        settings=ContextWindowSettings(),
        strategy=_NestedMutableStrategy(),
    )

    fork = tracker.fork_for_dispatch()

    assert fork.strategy is not tracker.strategy
    assert fork.strategy.events is not tracker.strategy.events
    fork.strategy.events[0].append("dispatch")
    assert tracker.strategy.events == [["prototype"]]


def test_context_tracker_fork_reports_uncopyable_custom_strategy() -> None:
    tracker = ContextWindowTracker(
        settings=ContextWindowSettings(),
        strategy=_UncopyableStrategy(),
    )

    with pytest.raises(RuntimeError, match="cannot isolate compaction strategy"):
        tracker.fork_for_dispatch()


def test_runner_fork_deep_copies_legacy_context_tracker() -> None:
    tracker = _LegacyTracker()
    runner = AgentRunner(
        AgentConfig(
            name="legacy-tracker-prototype",
            instruction="Return a valid answer.",
            model_name="governai:test",
            input_model=_AgentInput,
            output_model=_AgentOutput,
        ),
        DeterministicProviderAdapter([]),
        context_tracker=tracker,
    )

    fork = runner.fork_for_dispatch()

    assert fork.context_tracker is not tracker
    assert fork.context_tracker.events is not tracker.events
    fork.context_tracker.events[0].append("dispatch")
    assert tracker.events == [["prototype"]]

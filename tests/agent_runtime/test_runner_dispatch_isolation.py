from __future__ import annotations

from pydantic import BaseModel

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ToolAttachmentManifest,
)
from zeroth.core.context_window.models import ContextWindowSettings
from zeroth.core.context_window.tracker import ContextWindowTracker


class _AgentInput(BaseModel):
    query: str


class _AgentOutput(BaseModel):
    answer: str


class _CompactionStrategy:
    def __init__(self, provider: object, client: object) -> None:
        self.provider = provider
        self.client = client


def test_runner_fork_rebuilds_dispatch_state_and_preserves_safe_dependencies() -> None:
    strategy_provider = object()
    strategy_client = object()
    tracker = ContextWindowTracker(
        settings=ContextWindowSettings(
            max_context_tokens=8_000,
            summary_trigger_ratio=0.75,
            preserve_recent_messages_count=6,
        ),
        strategy=_CompactionStrategy(strategy_provider, strategy_client),
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
    assert fork_tracker.strategy.provider is strategy_provider
    assert fork_tracker.strategy.client is strategy_client
    assert fork_tracker.state.accumulated_tokens == 0
    assert fork_tracker.state.compaction_count == 0
    assert fork_tracker.state.last_compaction_strategy is None

    assert fork.provider is provider
    assert fork.memory_resolver is memory_resolver
    assert fork.budget_enforcer is budget_enforcer
    assert fork.tool_executor is tool_executor

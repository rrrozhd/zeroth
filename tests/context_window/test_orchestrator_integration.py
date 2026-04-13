"""Integration tests for orchestrator context window tracker injection.

Verifies that RuntimeOrchestrator creates ContextWindowTracker from
AgentNodeData.context_window settings and injects it into the runner
before dispatch, following the save/inject/restore pattern.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from zeroth.core.context_window import (
    ContextWindowSettings,
    ContextWindowTracker,
    LLMSummarizationStrategy,
    ObservationMaskingStrategy,
    TruncationStrategy,
)
from zeroth.core.graph.models import AgentNode, AgentNodeData
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
from zeroth.core.runs import Run, RunRepository, RunStatus


class SimpleInput(BaseModel):
    query: str


class SimpleOutput(BaseModel):
    answer: str


class RecordingAgentRunner:
    """Minimal runner that records injected attributes for test inspection."""

    def __init__(self) -> None:
        self.config = MagicMock()
        self.config.model_name = "gpt-4o"
        self.provider = MagicMock()
        self.memory_resolver = None
        self.budget_enforcer = None
        self.context_tracker = None
        self._run_called_with_tracker = None

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
        enforcement_context: Any = None,
    ) -> MagicMock:
        # Capture the context_tracker at run time
        self._run_called_with_tracker = self.context_tracker
        result = MagicMock()
        result.output_data = {"answer": "ok"}
        result.audit_record = {}
        return result


def _make_agent_node(
    *,
    node_id: str = "agent-1",
    context_window: ContextWindowSettings | None = None,
) -> AgentNode:
    """Build a minimal AgentNode for testing."""
    return AgentNode(
        node_id=node_id,
        graph_version_ref="graph:v1",
        agent=AgentNodeData(
            instruction="Test instruction",
            model_provider="openai/gpt-4o",
            context_window=context_window,
        ),
    )


def _make_run(node_id: str = "agent-1") -> Run:
    """Build a minimal Run for testing."""
    run = Run(
        graph_version_ref="graph:v1",
        deployment_ref="deploy-1",
        thread_id="",
        current_node_ids=[node_id],
        pending_node_ids=[],
        metadata={"last_output": {}},
    )
    run.status = RunStatus.RUNNING
    return run


def _make_orchestrator(
    runner: Any,
    *,
    context_window_enabled: bool = True,
) -> RuntimeOrchestrator:
    """Build a minimal RuntimeOrchestrator for testing."""
    run_repo = AsyncMock(spec=RunRepository)
    audit_repo = AsyncMock()
    return RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={"agent-1": runner},
        executable_unit_runner=MagicMock(),
        audit_repository=audit_repo,
        context_window_enabled=context_window_enabled,
    )


# ---------------------------------------------------------------------------
# Tracker injection with observation_masking (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injects_observation_masking_tracker() -> None:
    """Orchestrator creates ObservationMaskingStrategy when strategy is default."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    node = _make_agent_node(context_window=ContextWindowSettings())
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    # During run(), the tracker should have been injected
    assert runner._run_called_with_tracker is not None
    assert isinstance(runner._run_called_with_tracker, ContextWindowTracker)
    assert isinstance(runner._run_called_with_tracker.strategy, ObservationMaskingStrategy)


# ---------------------------------------------------------------------------
# Tracker injection with truncation strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injects_truncation_tracker() -> None:
    """Orchestrator creates TruncationStrategy when compaction_strategy is 'truncation'."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    settings = ContextWindowSettings(compaction_strategy="truncation")
    node = _make_agent_node(context_window=settings)
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    assert runner._run_called_with_tracker is not None
    assert isinstance(runner._run_called_with_tracker.strategy, TruncationStrategy)


# ---------------------------------------------------------------------------
# Tracker injection with llm_summarization strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injects_llm_summarization_tracker() -> None:
    """Orchestrator creates LLMSummarizationStrategy using runner's provider."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    settings = ContextWindowSettings(compaction_strategy="llm_summarization")
    node = _make_agent_node(context_window=settings)
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    assert runner._run_called_with_tracker is not None
    assert isinstance(runner._run_called_with_tracker.strategy, LLMSummarizationStrategy)


# ---------------------------------------------------------------------------
# No tracker injected when context_window is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tracker_when_context_window_none() -> None:
    """When AgentNodeData has context_window=None, no tracker is injected."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    node = _make_agent_node(context_window=None)
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    # context_tracker should remain None (not injected)
    assert runner._run_called_with_tracker is None


# ---------------------------------------------------------------------------
# No tracker when context_window_enabled is False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tracker_when_disabled() -> None:
    """When context_window_enabled=False, no tracker is injected even with settings."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner, context_window_enabled=False)
    node = _make_agent_node(context_window=ContextWindowSettings())
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    assert runner._run_called_with_tracker is None


# ---------------------------------------------------------------------------
# Tracker restored in finally block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracker_restored_after_dispatch() -> None:
    """After dispatch, the original context_tracker is restored on the runner."""
    runner = RecordingAgentRunner()
    original_tracker = runner.context_tracker  # None
    orchestrator = _make_orchestrator(runner)
    node = _make_agent_node(context_window=ContextWindowSettings())
    run = _make_run()

    await orchestrator._dispatch_node(node, run, {"query": "hi"})

    # After dispatch, the runner's context_tracker should be restored to original
    assert runner.context_tracker is original_tracker


# ---------------------------------------------------------------------------
# Context window state in audit record execution_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_window_state_in_audit() -> None:
    """Context window state appears in audit record execution_metadata."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    node = _make_agent_node(context_window=ContextWindowSettings())
    run = _make_run()

    _output, audit = await orchestrator._dispatch_node(node, run, {"query": "hi"})

    # The tracker was injected but maybe_compact wasn't called (no real LLM),
    # so accumulated_tokens should be 0 and compaction_count 0.
    assert "execution_metadata" in audit
    assert "context_window" in audit["execution_metadata"]
    cw = audit["execution_metadata"]["context_window"]
    assert cw["accumulated_tokens"] == 0
    assert cw["compaction_count"] == 0


# ---------------------------------------------------------------------------
# No context_window in audit when no tracker injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_context_window_in_audit_without_tracker() -> None:
    """When no tracker is injected, no context_window in audit execution_metadata."""
    runner = RecordingAgentRunner()
    orchestrator = _make_orchestrator(runner)
    node = _make_agent_node(context_window=None)
    run = _make_run()

    _output, audit = await orchestrator._dispatch_node(node, run, {"query": "hi"})

    # execution_metadata may or may not exist, but context_window should not
    exec_meta = audit.get("execution_metadata", {})
    assert "context_window" not in exec_meta


# ---------------------------------------------------------------------------
# AgentNodeData accepts ContextWindowSettings
# ---------------------------------------------------------------------------


def test_agent_node_data_accepts_context_window_settings() -> None:
    """AgentNodeData can be created with context_window field."""
    data = AgentNodeData(
        instruction="test",
        model_provider="openai/gpt-4o",
        context_window=ContextWindowSettings(
            max_context_tokens=64000,
            compaction_strategy="truncation",
        ),
    )
    assert data.context_window is not None
    assert data.context_window.max_context_tokens == 64000
    assert data.context_window.compaction_strategy == "truncation"


def test_agent_node_data_context_window_defaults_none() -> None:
    """AgentNodeData.context_window defaults to None."""
    data = AgentNodeData(
        instruction="test",
        model_provider="openai/gpt-4o",
    )
    assert data.context_window is None

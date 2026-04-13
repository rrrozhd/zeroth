"""Integration tests for AgentRunner with context window compaction.

Verifies that the runner correctly integrates with ContextWindowTracker
for pre-LLM compaction, thread state persistence of compacted messages,
and audit record enrichment.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime.models import (
    AgentConfig,
    AgentRunResult,
    InMemoryThreadStateStore,
)
from zeroth.core.agent_runtime.provider import (
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.agent_runtime.runner import AgentRunner


class SimpleInput(BaseModel):
    query: str


class SimpleOutput(BaseModel):
    answer: str


def _make_config(**overrides: Any) -> AgentConfig:
    defaults = {
        "name": "test-agent",
        "instruction": "Answer the question.",
        "model_name": "gpt-4o",
        "input_model": SimpleInput,
        "output_model": SimpleOutput,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_provider(content: str = '{"answer":"hello"}') -> DeterministicProviderAdapter:
    return DeterministicProviderAdapter([ProviderResponse(content=content)])


def _make_compaction_result(
    *,
    strategy_name: str = "observation_masking",
    tokens_before: int = 5000,
    tokens_after: int = 2000,
    original_count: int = 10,
    compacted_count: int = 6,
    archived_messages: list[Any] | None = None,
) -> MagicMock:
    """Build a mock CompactionResult with the given attributes."""
    result = MagicMock()
    result.strategy_name = strategy_name
    result.tokens_before = tokens_before
    result.tokens_after = tokens_after
    result.original_count = original_count
    result.compacted_count = compacted_count
    result.archived_messages = archived_messages
    result.messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
    ]
    return result


# ---------------------------------------------------------------------------
# Backward compatibility: context_tracker=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_works_without_context_tracker() -> None:
    """When context_tracker is None (default), runner works exactly as before."""
    provider = _make_provider()
    runner = AgentRunner(_make_config(), provider)

    assert runner.context_tracker is None

    result = await runner.run({"query": "hi"})
    assert result.output_data == {"answer": "hello"}
    assert "context_window" not in result.audit_record


# ---------------------------------------------------------------------------
# Compaction: maybe_compact called before LLM invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_calls_maybe_compact_before_llm() -> None:
    """When context_tracker is provided, maybe_compact is called with messages."""
    provider = _make_provider()
    tracker = AsyncMock()
    # Return messages unchanged, no compaction
    tracker.maybe_compact = AsyncMock(side_effect=lambda msgs, model: (msgs, None))

    runner = AgentRunner(_make_config(), provider, context_tracker=tracker)
    result = await runner.run({"query": "hi"})

    assert result.output_data == {"answer": "hello"}
    tracker.maybe_compact.assert_called()
    call_args = tracker.maybe_compact.call_args
    assert call_args[0][1] == "gpt-4o"  # model_name


# ---------------------------------------------------------------------------
# Compaction result metadata in audit record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_metadata_in_audit_record() -> None:
    """When compaction occurs, metadata appears in the audit record."""
    provider = _make_provider()
    compaction_result = _make_compaction_result()
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(
        return_value=(
            [{"role": "user", "content": "hello"}],
            compaction_result,
        )
    )

    runner = AgentRunner(_make_config(), provider, context_tracker=tracker)
    result = await runner.run({"query": "hi"})

    assert "context_window" in result.audit_record
    cw = result.audit_record["context_window"]
    assert cw["strategy"] == "observation_masking"
    assert cw["tokens_before"] == 5000
    assert cw["tokens_after"] == 2000
    assert cw["messages_before"] == 10
    assert cw["messages_after"] == 6


# ---------------------------------------------------------------------------
# Compacted messages persisted in thread state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compacted_messages_persisted_in_thread_state() -> None:
    """When compaction occurs, compacted_messages are stored in thread state."""
    provider = _make_provider()
    compacted_msgs = [{"role": "user", "content": "compacted"}]
    compaction_result = _make_compaction_result()
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(
        return_value=(compacted_msgs, compaction_result)
    )
    store = InMemoryThreadStateStore()

    runner = AgentRunner(
        _make_config(), provider,
        thread_state_store=store,
        context_tracker=tracker,
    )
    await runner.run({"query": "hi"}, thread_id="thread-1")

    state = await store.load("thread-1")
    assert state is not None
    assert "compacted_messages" in state
    assert state["compacted_messages"] == compacted_msgs


# ---------------------------------------------------------------------------
# Archived messages persisted in thread state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archived_messages_persisted_in_thread_state() -> None:
    """When compaction archives originals, archived_messages are stored in thread state."""
    provider = _make_provider()
    archived = [{"role": "user", "content": "old message"}]
    compacted_msgs = [{"role": "user", "content": "compacted"}]
    compaction_result = _make_compaction_result(archived_messages=archived)
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(
        return_value=(compacted_msgs, compaction_result)
    )
    store = InMemoryThreadStateStore()

    runner = AgentRunner(
        _make_config(), provider,
        thread_state_store=store,
        context_tracker=tracker,
    )
    await runner.run({"query": "hi"}, thread_id="thread-1")

    state = await store.load("thread-1")
    assert state is not None
    assert "archived_messages" in state
    assert state["archived_messages"] == archived


# ---------------------------------------------------------------------------
# Compacted messages restored from thread state on subsequent runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compacted_messages_restored_from_thread_state() -> None:
    """When thread state has compacted_messages, they are used as starting messages."""
    provider = DeterministicProviderAdapter([
        ProviderResponse(content='{"answer":"first"}'),
        ProviderResponse(content='{"answer":"second"}'),
    ])
    compacted_msgs = [{"role": "user", "content": "compacted-context"}]
    compaction_result = _make_compaction_result()
    tracker = AsyncMock()
    # First call: trigger compaction. Second call: no compaction needed.
    tracker.maybe_compact = AsyncMock(
        side_effect=[
            (compacted_msgs, compaction_result),
            (compacted_msgs, None),
        ]
    )
    store = InMemoryThreadStateStore()

    runner = AgentRunner(
        _make_config(), provider,
        thread_state_store=store,
        context_tracker=tracker,
    )

    # First run: compaction occurs and is persisted
    await runner.run({"query": "first"}, thread_id="thread-2")

    # Second run: compacted messages should be loaded from thread state
    result2 = await runner.run({"query": "second"}, thread_id="thread-2")
    assert result2.output_data == {"answer": "second"}

    # Verify the second call to maybe_compact used the compacted messages
    second_call_messages = tracker.maybe_compact.call_args_list[1][0][0]
    assert second_call_messages == compacted_msgs


# ---------------------------------------------------------------------------
# No compaction metadata in audit when no compaction occurs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_compaction_metadata_when_no_compaction() -> None:
    """When context_tracker is present but no compaction is needed, no metadata in audit."""
    provider = _make_provider()
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(side_effect=lambda msgs, model: (msgs, None))

    runner = AgentRunner(_make_config(), provider, context_tracker=tracker)
    result = await runner.run({"query": "hi"})

    assert "context_window" not in result.audit_record


# ---------------------------------------------------------------------------
# No compacted_messages in thread state when no compaction occurs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_compacted_messages_in_state_without_compaction() -> None:
    """When no compaction occurs, thread state has no compacted_messages key."""
    provider = _make_provider()
    tracker = AsyncMock()
    tracker.maybe_compact = AsyncMock(side_effect=lambda msgs, model: (msgs, None))
    store = InMemoryThreadStateStore()

    runner = AgentRunner(
        _make_config(), provider,
        thread_state_store=store,
        context_tracker=tracker,
    )
    await runner.run({"query": "hi"}, thread_id="thread-3")

    state = await store.load("thread-3")
    assert state is not None
    assert "compacted_messages" not in state
    assert "archived_messages" not in state


# ---------------------------------------------------------------------------
# Compaction runs inside _resolve_tool_calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_in_tool_call_resolution() -> None:
    """Compaction runs between tool call re-invocations."""
    # First response has a tool call, second response is the final answer
    tool_call_response = ProviderResponse(
        content=None,
        tool_calls=[{"id": "tc-1", "name": "search", "args": {"q": "test"}}],
    )
    final_response = ProviderResponse(content='{"answer":"found it"}')
    provider = DeterministicProviderAdapter([tool_call_response, final_response])

    tracker = AsyncMock()
    call_count = 0

    async def track_compact(msgs: list[Any], model: str) -> tuple[list[Any], Any]:
        nonlocal call_count
        call_count += 1
        return msgs, None

    tracker.maybe_compact = AsyncMock(side_effect=track_compact)

    # Need a tool executor for tool calls
    async def tool_executor(binding: Any, args: Any) -> dict[str, str]:
        return {"result": "search result"}

    from zeroth.core.agent_runtime.tools import ToolAttachmentManifest

    tool_manifest = ToolAttachmentManifest(
        alias="search",
        executable_unit_ref="tools:search",
        parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    config = _make_config(
        tool_attachments=[tool_manifest],
        max_tool_calls=4,
    )
    runner = AgentRunner(
        config, provider,
        tool_executor=tool_executor,
        context_tracker=tracker,
    )
    result = await runner.run({"query": "hi"})
    assert result.output_data == {"answer": "found it"}

    # maybe_compact called at least twice: once before first LLM call,
    # once inside tool call resolution before re-invocation
    assert call_count >= 2

"""Tests for ContextWindowTracker."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from zeroth.core.context_window.errors import TokenCountError
from zeroth.core.context_window.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)
from zeroth.core.context_window.tracker import ContextWindowTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(count: int) -> list[dict[str, Any]]:
    """Create a list of simple user messages."""
    return [{"role": "user", "content": f"message {i}"} for i in range(count)]


class _FakeStrategy:
    """Minimal strategy that satisfies the Protocol for testing."""

    async def compact(
        self,
        messages: list[Any],
        *,
        settings: ContextWindowSettings,
        model_name: str,
    ) -> CompactionResult:
        return CompactionResult(
            messages=messages[:2],
            original_count=len(messages),
            compacted_count=2,
            tokens_before=1000,
            tokens_after=200,
            strategy_name="fake",
        )


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    """Tests for ContextWindowTracker.count_tokens."""

    def test_returns_token_count_from_litellm(self) -> None:
        settings = ContextWindowSettings()
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = [{"role": "user", "content": "hello world"}]
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 42
            result = tracker.count_tokens(msgs, "gpt-4o")
        assert result == 42
        mock_litellm.token_counter.assert_called_once()

    def test_empty_messages_returns_zero(self) -> None:
        settings = ContextWindowSettings()
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        result = tracker.count_tokens([], "gpt-4o")
        assert result == 0

    def test_wraps_litellm_exception_in_token_count_error(self) -> None:
        settings = ContextWindowSettings()
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = [{"role": "user", "content": "hello"}]
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = RuntimeError("litellm broke")
            with pytest.raises(TokenCountError, match="token counting failed"):
                tracker.count_tokens(msgs, "gpt-4o")

    def test_counts_raw_tool_call_messages_without_raising(self) -> None:
        # B3: assistant messages carrying tool_calls in the {name,args,id} form
        # (what the agent runner emits) previously made the real
        # litellm.token_counter raise "must contain a function key", hard-failing
        # every tool-using node at maybe_compact's unconditional count_tokens.
        settings = ContextWindowSettings()
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = [
            {"role": "user", "content": "search for cats"},
            {
                "role": "assistant",
                "tool_calls": [{"name": "search", "args": {"q": "cats"}, "id": "call_1"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "found 3"},
        ]
        # Real litellm (no mock): must not raise.
        assert tracker.count_tokens(msgs, "gpt-4o") > 0

    def test_normalize_reshapes_tool_calls_to_openai_form(self) -> None:
        normalized = ContextWindowTracker._normalize_messages(
            [{"role": "assistant", "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "c1"}]}]
        )
        assert normalized[0]["role"] == "assistant"
        assert normalized[0]["tool_calls"][0] == {
            "id": "c1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "x"}'},
        }

    def test_normalize_leaves_openai_tool_calls_untouched(self) -> None:
        openai_tc = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        normalized = ContextWindowTracker._normalize_messages(
            [{"role": "assistant", "tool_calls": [openai_tc]}]
        )
        assert normalized[0]["tool_calls"][0] == openai_tc


# ---------------------------------------------------------------------------
# needs_compaction
# ---------------------------------------------------------------------------


class TestNeedsCompaction:
    """Tests for ContextWindowTracker.needs_compaction."""

    def test_true_when_above_threshold(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        assert tracker.needs_compaction(800) is True
        assert tracker.needs_compaction(900) is True

    def test_false_when_below_threshold(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        assert tracker.needs_compaction(799) is False
        assert tracker.needs_compaction(100) is False

    def test_false_when_disabled(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=0)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        assert tracker.needs_compaction(99999) is False

    def test_exact_threshold(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        assert tracker.needs_compaction(800) is True


# ---------------------------------------------------------------------------
# maybe_compact
# ---------------------------------------------------------------------------


class TestMaybeCompact:
    """Tests for ContextWindowTracker.maybe_compact."""

    @pytest.mark.asyncio
    async def test_returns_original_when_below_threshold(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=10000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = _make_messages(3)
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 100  # well below threshold
            result_msgs, result_info = await tracker.maybe_compact(msgs, "gpt-4o")
        assert result_msgs == msgs
        assert result_info is None

    @pytest.mark.asyncio
    async def test_calls_strategy_when_above_threshold(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        strategy = _FakeStrategy()
        tracker = ContextWindowTracker(settings=settings, strategy=strategy)
        msgs = _make_messages(5)
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 900  # above threshold
            result_msgs, result_info = await tracker.maybe_compact(msgs, "gpt-4o")
        assert result_info is not None
        assert result_info.strategy_name == "fake"
        assert result_info.original_count == 5
        assert result_info.compacted_count == 2

    @pytest.mark.asyncio
    async def test_updates_accumulated_tokens_after_compaction(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = _make_messages(5)
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 900
            await tracker.maybe_compact(msgs, "gpt-4o")
        # _FakeStrategy returns tokens_after=200
        assert tracker.state.accumulated_tokens == 200


# ---------------------------------------------------------------------------
# state property
# ---------------------------------------------------------------------------


class TestState:
    """Tests for ContextWindowTracker.state property."""

    def test_initial_state(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=128_000)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        state = tracker.state
        assert isinstance(state, CompactionState)
        assert state.accumulated_tokens == 0
        assert state.max_tokens == 128_000
        assert state.compaction_count == 0
        assert state.last_compaction_strategy is None

    @pytest.mark.asyncio
    async def test_state_after_compaction(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=1000, summary_trigger_ratio=0.8)
        tracker = ContextWindowTracker(settings=settings, strategy=_FakeStrategy())
        msgs = _make_messages(5)
        with patch("zeroth.core.context_window.tracker.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 900
            await tracker.maybe_compact(msgs, "gpt-4o")
        state = tracker.state
        assert state.compaction_count == 1
        assert state.accumulated_tokens == 200
        assert state.last_compaction_strategy == "fake"

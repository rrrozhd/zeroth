"""Tests for compaction strategies."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.core.context_window.errors import CompactionError
from zeroth.core.context_window.models import (
    CompactionResult,
    ContextWindowSettings,
)
from zeroth.core.context_window.strategies import (
    CompactionStrategy,
    LLMSummarizationStrategy,
    ObservationMaskingStrategy,
    TruncationStrategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sys(content: str = "You are a helpful assistant.") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _tool(content: str, tool_call_id: str = "call_1") -> dict[str, Any]:
    return {"role": "tool", "content": content, "tool_call_id": tool_call_id}


def _settings(
    preserve: int = 2,
    archive: bool = False,
    max_tokens: int = 128_000,
) -> ContextWindowSettings:
    return ContextWindowSettings(
        max_context_tokens=max_tokens,
        preserve_recent_messages_count=preserve,
        archive_originals=archive,
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestCompactionStrategyProtocol:
    """Tests that all strategies satisfy the CompactionStrategy Protocol."""

    def test_protocol_is_runtime_checkable(self) -> None:
        assert isinstance(TruncationStrategy(), CompactionStrategy)

    def test_truncation_satisfies_protocol(self) -> None:
        assert isinstance(TruncationStrategy(), CompactionStrategy)

    def test_observation_masking_satisfies_protocol(self) -> None:
        assert isinstance(ObservationMaskingStrategy(), CompactionStrategy)

    def test_llm_summarization_satisfies_protocol(self) -> None:
        mock_provider = AsyncMock()
        assert isinstance(LLMSummarizationStrategy(provider=mock_provider), CompactionStrategy)


# ---------------------------------------------------------------------------
# TruncationStrategy
# ---------------------------------------------------------------------------


class TestTruncationStrategy:
    """Tests for TruncationStrategy."""

    @pytest.mark.asyncio
    async def test_preserves_system_and_recent(self) -> None:
        """System message + last N messages kept, middle dropped."""
        messages = [
            _sys(),
            _user("msg 1"),
            _assistant("resp 1"),
            _user("msg 2"),
            _assistant("resp 2"),
            _user("msg 3"),
            _assistant("resp 3"),
            _user("msg 4"),
            _assistant("resp 4"),
            _user("msg 5"),
        ]
        settings = _settings(preserve=2)
        strategy = TruncationStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 100
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.strategy_name == "truncation"
        assert result.original_count == 10
        # System + last 2 messages
        assert result.compacted_count == 3
        assert result.messages[0] == _sys()
        assert result.messages[-1] == messages[-1]
        assert result.messages[-2] == messages[-2]

    @pytest.mark.asyncio
    async def test_archive_originals(self) -> None:
        """When archive_originals is True, dropped messages are archived."""
        messages = [
            _sys(),
            _user("old 1"),
            _assistant("old resp"),
            _user("recent 1"),
            _assistant("recent resp"),
        ]
        settings = _settings(preserve=2, archive=True)
        strategy = TruncationStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 50
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.archived_messages is not None
        assert len(result.archived_messages) == 2  # old 1, old resp
        assert result.archived_messages[0] == _user("old 1")
        assert result.archived_messages[1] == _assistant("old resp")

    @pytest.mark.asyncio
    async def test_no_compaction_needed(self) -> None:
        """When all messages fit in preserve window, returns all unchanged."""
        messages = [_sys(), _user("hi"), _assistant("hello")]
        settings = _settings(preserve=4)
        strategy = TruncationStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 30
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.compacted_count == 3
        assert result.messages == messages

    @pytest.mark.asyncio
    async def test_only_system_plus_recent(self) -> None:
        """Edge case: system + exactly N recent -- nothing to drop."""
        messages = [_sys(), _user("a"), _assistant("b")]
        settings = _settings(preserve=2)
        strategy = TruncationStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 20
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.compacted_count == 3
        assert result.messages == messages

    @pytest.mark.asyncio
    async def test_returns_new_list_not_mutating_original(self) -> None:
        """T-37-01: compaction must return a new list, never mutate original."""
        messages = [_sys(), _user("old"), _user("new 1"), _user("new 2")]
        original_copy = list(messages)
        settings = _settings(preserve=2)
        strategy = TruncationStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 40
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert messages == original_copy  # Original not mutated
        assert result.messages is not messages  # New list identity


# ---------------------------------------------------------------------------
# ObservationMaskingStrategy
# ---------------------------------------------------------------------------


class TestObservationMaskingStrategy:
    """Tests for ObservationMaskingStrategy."""

    @pytest.mark.asyncio
    async def test_masks_tool_output_in_older_messages(self) -> None:
        """Tool messages outside preserve window get their content masked."""
        messages = [
            _sys(),
            _user("call the tool"),
            _tool("here is a very long tool output with lots of data"),
            _user("recent 1"),
            _assistant("recent resp"),
        ]
        settings = _settings(preserve=2)
        strategy = ObservationMaskingStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = [
                50,  # tokens_before (full list)
                12,  # per-message token count for the tool message
                30,  # tokens_after (after masking)
            ]
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.strategy_name == "observation_masking"
        # The tool message (index 2 in original, index 2 in result) should be masked
        masked_msg = result.messages[2]
        assert "[output omitted" in masked_msg["content"]
        assert "12 tokens" in masked_msg["content"]

    @pytest.mark.asyncio
    async def test_preserves_recent_messages_untouched(self) -> None:
        """Messages within the preserve window are not modified."""
        messages = [
            _sys(),
            _tool("old tool output"),
            _user("recent q"),
            _tool("recent tool output", tool_call_id="call_2"),
        ]
        settings = _settings(preserve=2)
        strategy = ObservationMaskingStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = [40, 8, 25]
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        # Recent messages (last 2) should be untouched
        assert result.messages[-1] == messages[-1]
        assert result.messages[-2] == messages[-2]

    @pytest.mark.asyncio
    async def test_non_tool_older_messages_unchanged(self) -> None:
        """Regular user/assistant messages in the old section stay unchanged."""
        messages = [
            _sys(),
            _user("old question"),
            _assistant("old answer"),
            _user("recent q"),
            _assistant("recent a"),
        ]
        settings = _settings(preserve=2)
        strategy = ObservationMaskingStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = [50, 30]
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        # Non-tool messages should be unchanged
        assert result.messages[1] == _user("old question")
        assert result.messages[2] == _assistant("old answer")

    @pytest.mark.asyncio
    async def test_archive_originals(self) -> None:
        """When archive_originals=True, original unmasked messages stored."""
        messages = [
            _sys(),
            _tool("big output"),
            _user("recent"),
            _assistant("resp"),
        ]
        settings = _settings(preserve=2, archive=True)
        strategy = ObservationMaskingStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = [40, 10, 20]
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.archived_messages is not None
        # Archived messages are the originals from the middle section
        assert any(m.get("content") == "big output" for m in result.archived_messages)

    @pytest.mark.asyncio
    async def test_returns_new_list(self) -> None:
        """T-37-01: must not mutate original list."""
        messages = [
            _sys(),
            _tool("output"),
            _user("recent"),
            _assistant("resp"),
        ]
        original_copy = list(messages)
        settings = _settings(preserve=2)
        strategy = ObservationMaskingStrategy()
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.side_effect = [40, 8, 25]
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert messages == original_copy
        assert result.messages is not messages


# ---------------------------------------------------------------------------
# LLMSummarizationStrategy
# ---------------------------------------------------------------------------


class TestLLMSummarizationStrategy:
    """Tests for LLMSummarizationStrategy."""

    @pytest.mark.asyncio
    async def test_calls_provider_and_returns_summary(self) -> None:
        """Strategy calls provider.ainvoke with summarization prompt."""
        mock_provider = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Summary of previous conversation."
        mock_provider.ainvoke.return_value = mock_response

        messages = [
            _sys(),
            _user("old question 1"),
            _assistant("old answer 1"),
            _user("old question 2"),
            _assistant("old answer 2"),
            _user("recent q"),
            _assistant("recent a"),
        ]
        settings = _settings(preserve=2)
        strategy = LLMSummarizationStrategy(provider=mock_provider)
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 50
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.strategy_name == "llm_summarization"
        mock_provider.ainvoke.assert_called_once()
        # Result should contain system + summary + recent messages
        assert "[Previous conversation summary]" in result.messages[1]["content"]
        assert result.messages[-1] == messages[-1]
        assert result.messages[-2] == messages[-2]
        # System message preserved
        assert result.messages[0] == _sys()

    @pytest.mark.asyncio
    async def test_wraps_provider_error_in_compaction_error(self) -> None:
        """Provider failure gets wrapped in CompactionError."""
        mock_provider = AsyncMock()
        mock_provider.ainvoke.side_effect = RuntimeError("LLM down")

        messages = [
            _sys(),
            _user("old"),
            _assistant("old resp"),
            _user("recent"),
            _assistant("recent resp"),
        ]
        settings = _settings(preserve=2)
        strategy = LLMSummarizationStrategy(provider=mock_provider)
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 50
            with pytest.raises(CompactionError, match="summarization failed"):
                await strategy.compact(messages, settings=settings, model_name="gpt-4o")

    @pytest.mark.asyncio
    async def test_archive_originals(self) -> None:
        """When archive_originals=True, original messages are archived."""
        mock_provider = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Summary."
        mock_provider.ainvoke.return_value = mock_response

        messages = [
            _sys(),
            _user("old 1"),
            _assistant("old resp 1"),
            _user("recent"),
            _assistant("recent resp"),
        ]
        settings = _settings(preserve=2, archive=True)
        strategy = LLMSummarizationStrategy(provider=mock_provider)
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 30
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert result.archived_messages is not None
        assert len(result.archived_messages) == 2
        assert result.archived_messages[0] == _user("old 1")

    @pytest.mark.asyncio
    async def test_returns_new_list(self) -> None:
        """T-37-01: must not mutate original messages."""
        mock_provider = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Summary."
        mock_provider.ainvoke.return_value = mock_response

        messages = [
            _sys(),
            _user("old"),
            _user("recent 1"),
            _assistant("recent 2"),
        ]
        original_copy = list(messages)
        settings = _settings(preserve=2)
        strategy = LLMSummarizationStrategy(provider=mock_provider)
        with patch("zeroth.core.context_window.strategies.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 30
            result = await strategy.compact(messages, settings=settings, model_name="gpt-4o")

        assert messages == original_copy
        assert result.messages is not messages

"""Context window tracker for token counting and compaction coordination.

The ContextWindowTracker wraps litellm.token_counter to count message
tokens, detects when the context window is nearing capacity, and
delegates to a CompactionStrategy to reduce message size.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import litellm

from zeroth.core.context_window.errors import TokenCountError
from zeroth.core.context_window.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)

if TYPE_CHECKING:
    from zeroth.core.context_window.strategies import CompactionStrategy


class ContextWindowTracker:
    """Tracks token usage and coordinates compaction when thresholds are exceeded.

    Uses litellm.token_counter for accurate, model-specific token counting.
    When accumulated tokens reach the configured ratio of max_context_tokens,
    delegates to the provided CompactionStrategy to reduce message size.
    """

    def __init__(
        self,
        settings: ContextWindowSettings,
        strategy: CompactionStrategy,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self._accumulated_tokens: int = 0
        self._compaction_count: int = 0
        self._last_strategy_name: str | None = None

    def count_tokens(self, messages: list[Any], model_name: str) -> int:
        """Count the total tokens in a message list using litellm.

        Returns 0 for an empty message list. Wraps any litellm exception
        in a TokenCountError so callers do not need to depend on litellm
        exception types.
        """
        if not messages:
            return 0
        try:
            normalized = self._normalize_messages(messages)
            return litellm.token_counter(model=model_name, messages=normalized)
        except Exception as exc:
            msg = f"token counting failed for model {model_name}: {exc}"
            raise TokenCountError(msg) from exc

    def needs_compaction(self, token_count: int) -> bool:
        """Return True when the token count has reached the compaction threshold.

        Returns False when max_context_tokens is 0 (compaction disabled).
        """
        if self.settings.max_context_tokens <= 0:
            return False
        ratio = token_count / self.settings.max_context_tokens
        return ratio >= self.settings.summary_trigger_ratio

    async def maybe_compact(
        self,
        messages: list[Any],
        model_name: str,
    ) -> tuple[list[Any], CompactionResult | None]:
        """Compact messages if the token count exceeds the threshold.

        Returns a tuple of (messages, CompactionResult | None). When
        compaction is not needed, returns the original messages and None.
        """
        token_count = self.count_tokens(messages, model_name)
        self._accumulated_tokens = token_count
        if not self.needs_compaction(token_count):
            return messages, None
        result = await self.strategy.compact(
            messages,
            settings=self.settings,
            model_name=model_name,
        )
        self._accumulated_tokens = result.tokens_after
        self._compaction_count += 1
        self._last_strategy_name = result.strategy_name
        return list(result.messages), result

    @property
    def state(self) -> CompactionState:
        """Return the current compaction state."""
        return CompactionState(
            accumulated_tokens=self._accumulated_tokens,
            max_tokens=self.settings.max_context_tokens,
            compaction_count=self._compaction_count,
            last_compaction_strategy=self._last_strategy_name,
        )

    @staticmethod
    def _normalize_messages(messages: list[Any]) -> list[dict[str, Any]]:
        """Convert messages to dicts for litellm compatibility.

        Handles Pydantic models (via model_dump), plain dicts, and
        other objects by passing them through unchanged.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if hasattr(msg, "model_dump"):
                result.append(msg.model_dump())
            elif isinstance(msg, dict):
                result.append(msg)
            else:
                result.append({"role": "user", "content": str(msg)})
        return result

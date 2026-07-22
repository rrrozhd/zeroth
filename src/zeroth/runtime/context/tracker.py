"""Context window tracker for token counting and compaction coordination.

The ContextWindowTracker wraps litellm.token_counter to count message
tokens, detects when the context window is nearing capacity, and
delegates to a CompactionStrategy to reduce message size.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import litellm

from zeroth.runtime.context.errors import TokenCountError
from zeroth.runtime.context.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)

if TYPE_CHECKING:
    from zeroth.runtime.context.strategies import CompactionStrategy

# LangChain message ``type`` -> OpenAI ``role`` (litellm speaks OpenAI).
_TYPE_TO_ROLE = {"ai": "assistant", "human": "user", "system": "system", "tool": "tool"}


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

    def fork_for_dispatch(self) -> ContextWindowTracker:
        """Create a tracker with copied configuration and fresh run state."""
        strategy_fork = getattr(self.strategy, "fork_for_dispatch", None)
        try:
            strategy = strategy_fork() if callable(strategy_fork) else deepcopy(self.strategy)
        except Exception as exc:
            strategy_name = type(self.strategy).__name__
            raise RuntimeError(
                f"cannot isolate compaction strategy {strategy_name} for dispatch"
            ) from exc
        return ContextWindowTracker(
            settings=deepcopy(self.settings),
            strategy=strategy,
        )

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
        """Convert messages to the OpenAI dict shape litellm expects.

        Handles Pydantic models (via model_dump), plain dicts, and other objects.
        Crucially, tool-call messages are reshaped to the OpenAI form (audit B3):
        LangChain/agent tool calls arrive as ``{"name", "args", "id"}`` (or a raw
        ``{"role":"assistant","tool_calls":[...]}`` with no ``function`` key), and
        ``litellm.token_counter`` rejects those with "must contain a function
        key" — hard-failing every tool-using node at the unconditional
        count_tokens call in ``maybe_compact``.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if hasattr(msg, "model_dump"):
                result.append(ContextWindowTracker._normalize_dict(msg.model_dump()))
            elif isinstance(msg, dict):
                result.append(ContextWindowTracker._normalize_dict(msg))
            else:
                result.append({"role": "user", "content": str(msg)})
        return result

    @staticmethod
    def _normalize_dict(data: dict[str, Any]) -> dict[str, Any]:
        """Map a message dict to the OpenAI role/tool_call shape litellm accepts."""
        out = dict(data)
        # Derive an OpenAI role from a LangChain `type` when role is absent.
        if not out.get("role") and out.get("type") in _TYPE_TO_ROLE:
            out["role"] = _TYPE_TO_ROLE[out["type"]]
        tool_calls = out.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            out["tool_calls"] = [ContextWindowTracker._normalize_tool_call(tc) for tc in tool_calls]
            # litellm requires the tool-call carrier to be an assistant turn.
            out["role"] = "assistant"
        return out

    @staticmethod
    def _normalize_tool_call(tool_call: Any) -> Any:
        """Reshape a single tool call to OpenAI ``{id,type,function{name,arguments}}``."""
        if not isinstance(tool_call, dict) or "function" in tool_call:
            return tool_call  # already OpenAI-shaped (or not a dict) — leave as is
        args = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(args, str):
            args = json.dumps(args)
        return {
            "id": tool_call.get("id", ""),
            "type": "function",
            "function": {"name": tool_call.get("name", ""), "arguments": args},
        }

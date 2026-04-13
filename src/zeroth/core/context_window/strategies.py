"""Compaction strategies for context window management.

Provides three built-in strategies that all satisfy the CompactionStrategy
Protocol:

- **TruncationStrategy** -- drops oldest middle messages, keeping system and
  recent N messages.
- **ObservationMaskingStrategy** -- replaces tool output content in older
  messages with approximate token-count placeholders.
- **LLMSummarizationStrategy** -- condenses older messages into a single
  summary via a ProviderAdapter LLM call.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import litellm

from zeroth.core.context_window.errors import CompactionError
from zeroth.core.context_window.models import (
    CompactionResult,
    ContextWindowSettings,
)


@runtime_checkable
class CompactionStrategy(Protocol):
    """Interface that all compaction strategies must satisfy.

    Each strategy receives the full message list, settings, and the model
    name, and returns a CompactionResult with a (possibly shorter) message
    list plus metrics about what changed.
    """

    async def compact(
        self,
        messages: list[Any],
        *,
        settings: ContextWindowSettings,
        model_name: str,
    ) -> CompactionResult: ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_role(msg: Any) -> str | None:
    """Extract the role from a message (dict or Pydantic model)."""
    if isinstance(msg, dict):
        return msg.get("role")
    return getattr(msg, "role", None)


def _get_content(msg: Any) -> str:
    """Extract string content from a message."""
    if isinstance(msg, dict):
        return str(msg.get("content", ""))
    return str(getattr(msg, "content", ""))


def _has_tool_call_id(msg: Any) -> bool:
    """Check whether a message has a tool_call_id field."""
    if isinstance(msg, dict):
        return "tool_call_id" in msg
    return hasattr(msg, "tool_call_id")


def _split_messages(
    messages: list[Any],
    preserve_count: int,
) -> tuple[dict[str, Any] | None, list[Any], list[Any]]:
    """Split messages into system, middle, and recent sections.

    Returns (system_msg | None, middle_msgs, recent_msgs).
    System message is messages[0] if its role is "system".
    Recent is the last ``preserve_count`` messages (excluding the system
    message). Middle is everything between system and recent.
    """
    if not messages:
        return None, [], []

    system_msg: dict[str, Any] | None = None
    start = 0

    if _get_role(messages[0]) == "system":
        system_msg = messages[0] if isinstance(messages[0], dict) else dict(messages[0])
        start = 1

    remaining = messages[start:]

    if preserve_count >= len(remaining):
        return system_msg, [], list(remaining)

    middle = remaining[: len(remaining) - preserve_count]
    recent = remaining[len(remaining) - preserve_count :]
    return system_msg, list(middle), list(recent)


def _count_tokens_safe(messages: list[Any], model_name: str) -> int:
    """Count tokens via litellm, returning 0 on empty input."""
    if not messages:
        return 0
    normalized = []
    for msg in messages:
        if isinstance(msg, dict):
            normalized.append(msg)
        elif hasattr(msg, "model_dump"):
            normalized.append(msg.model_dump())
        else:
            normalized.append({"role": "user", "content": str(msg)})
    return litellm.token_counter(model=model_name, messages=normalized)


# ---------------------------------------------------------------------------
# TruncationStrategy
# ---------------------------------------------------------------------------


class TruncationStrategy:
    """Drops oldest middle messages, preserving system and recent N.

    The simplest compaction strategy: keeps the system message at index 0
    and the last ``preserve_recent_messages_count`` messages, dropping
    everything in between.
    """

    async def compact(
        self,
        messages: list[Any],
        *,
        settings: ContextWindowSettings,
        model_name: str,
    ) -> CompactionResult:
        """Truncate messages by dropping the middle section."""
        system_msg, middle, recent = _split_messages(
            messages, settings.preserve_recent_messages_count
        )

        tokens_before = _count_tokens_safe(messages, model_name)

        # Build compacted list
        compacted: list[Any] = []
        if system_msg is not None:
            compacted.append(system_msg)
        compacted.extend(recent)

        # If nothing was dropped, return all messages unchanged
        if not middle:
            compacted_all: list[Any] = []
            if system_msg is not None:
                compacted_all.append(system_msg)
            compacted_all.extend(recent)
            return CompactionResult(
                messages=compacted_all,
                original_count=len(messages),
                compacted_count=len(compacted_all),
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                strategy_name="truncation",
                archived_messages=None,
            )

        tokens_after = _count_tokens_safe(compacted, model_name)

        archived = list(middle) if settings.archive_originals else None

        return CompactionResult(
            messages=compacted,
            original_count=len(messages),
            compacted_count=len(compacted),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            strategy_name="truncation",
            archived_messages=archived,
        )


# ---------------------------------------------------------------------------
# ObservationMaskingStrategy
# ---------------------------------------------------------------------------


class ObservationMaskingStrategy:
    """Replaces tool output content in older messages with placeholders.

    For each message outside the preserve window that has role "tool" or
    a ``tool_call_id`` field, replaces its content with a placeholder
    showing the approximate token count. This preserves the message
    structure (so tool-call/result pairs remain valid) while dramatically
    reducing token usage from large tool outputs.
    """

    async def compact(
        self,
        messages: list[Any],
        *,
        settings: ContextWindowSettings,
        model_name: str,
    ) -> CompactionResult:
        """Mask tool output content in older messages."""
        system_msg, middle, recent = _split_messages(
            messages, settings.preserve_recent_messages_count
        )

        tokens_before = _count_tokens_safe(messages, model_name)

        archived_originals: list[Any] = []
        masked_middle: list[Any] = []

        for msg in middle:
            is_tool = _get_role(msg) == "tool" or _has_tool_call_id(msg)
            if is_tool:
                if settings.archive_originals:
                    archived_originals.append(dict(msg) if isinstance(msg, dict) else msg)
                # Count tokens in the original content
                content = _get_content(msg)
                single_msg = [{"role": "user", "content": content}]
                token_count = litellm.token_counter(model=model_name, messages=single_msg)
                placeholder = f"[output omitted -- {token_count} tokens]"
                masked = dict(msg) if isinstance(msg, dict) else {}
                masked["content"] = placeholder
                masked["role"] = _get_role(msg) or "tool"
                if _has_tool_call_id(msg):
                    if isinstance(msg, dict):
                        masked["tool_call_id"] = msg["tool_call_id"]
                    else:
                        masked["tool_call_id"] = msg.tool_call_id
                masked_middle.append(masked)
            else:
                if settings.archive_originals:
                    archived_originals.append(dict(msg) if isinstance(msg, dict) else msg)
                masked_middle.append(dict(msg) if isinstance(msg, dict) else msg)

        # Build compacted list
        compacted: list[Any] = []
        if system_msg is not None:
            compacted.append(system_msg)
        compacted.extend(masked_middle)
        compacted.extend(recent)

        tokens_after = _count_tokens_safe(compacted, model_name)

        return CompactionResult(
            messages=compacted,
            original_count=len(messages),
            compacted_count=len(compacted),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            strategy_name="observation_masking",
            archived_messages=archived_originals if settings.archive_originals else None,
        )


# ---------------------------------------------------------------------------
# LLMSummarizationStrategy
# ---------------------------------------------------------------------------


class LLMSummarizationStrategy:
    """Condenses older messages into a single summary via an LLM call.

    Uses a ProviderAdapter to call an LLM with a summarization prompt.
    The older messages are replaced with a single system message
    containing the summary.
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def compact(
        self,
        messages: list[Any],
        *,
        settings: ContextWindowSettings,
        model_name: str,
    ) -> CompactionResult:
        """Summarize older messages via an LLM call."""
        from zeroth.core.agent_runtime.provider import ProviderRequest

        system_msg, middle, recent = _split_messages(
            messages, settings.preserve_recent_messages_count
        )

        tokens_before = _count_tokens_safe(messages, model_name)

        # Build the summarization prompt
        conversation_text = "\n".join(
            f"{_get_role(m) or 'unknown'}: {_get_content(m)}" for m in middle
        )
        summarization_prompt = [
            {
                "role": "system",
                "content": (
                    "Summarize the following conversation concisely, "
                    "preserving key decisions, tool results, and context:"
                ),
            },
            {"role": "user", "content": conversation_text},
        ]

        try:
            request = ProviderRequest(
                model_name=model_name,
                messages=summarization_prompt,
            )
            response = await self._provider.ainvoke(request)
            summary_content = str(response.content)
        except Exception as exc:
            msg = f"summarization failed for model {model_name}: {exc}"
            raise CompactionError(msg) from exc

        summary_message: dict[str, Any] = {
            "role": "system",
            "content": f"[Previous conversation summary]: {summary_content}",
        }

        # Build compacted list
        compacted: list[Any] = []
        if system_msg is not None:
            compacted.append(system_msg)
        compacted.append(summary_message)
        compacted.extend(recent)

        tokens_after = _count_tokens_safe(compacted, model_name)

        archived = list(middle) if settings.archive_originals else None

        return CompactionResult(
            messages=compacted,
            original_count=len(messages),
            compacted_count=len(compacted),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            strategy_name="llm_summarization",
            archived_messages=archived,
        )

"""Pydantic models for the context window management subsystem.

Defines the core data shapes: ContextWindowSettings for per-node
configuration, CompactionResult for compaction output, and
CompactionState for tracking compaction history.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextWindowSettings(BaseModel):
    """Per-node context window configuration.

    Controls when and how the tracker compacts messages to stay within
    the model's context window limit.

    Fields:
        max_context_tokens: Maximum context window size in tokens.
            Set to 0 to disable compaction entirely.
        summary_trigger_ratio: Ratio of accumulated tokens to max that
            triggers compaction. Must be > 0 and <= 1.
        compaction_strategy: Name of the strategy to use for compaction.
        preserve_recent_messages_count: Number of recent messages to keep
            untouched during compaction.
        archive_originals: When True, compaction strategies store the
            original (dropped/modified) messages in CompactionResult.
    """

    model_config = ConfigDict(extra="forbid")

    max_context_tokens: int = Field(default=128_000, ge=0)
    summary_trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    compaction_strategy: str = "observation_masking"
    preserve_recent_messages_count: int = Field(default=4, ge=0)
    archive_originals: bool = False


class CompactionResult(BaseModel):
    """The result of a compaction operation.

    Captures the compacted message list alongside metrics about what
    changed and which strategy was used.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[Any]
    original_count: int
    compacted_count: int
    tokens_before: int
    tokens_after: int
    strategy_name: str
    archived_messages: list[Any] | None = None


class CompactionState(BaseModel):
    """Tracks the current state of context window compaction.

    Used by the tracker to report its internal counters and history.
    """

    model_config = ConfigDict(extra="forbid")

    accumulated_tokens: int = 0
    max_tokens: int = 0
    compaction_count: int = 0
    last_compaction_strategy: str | None = None

"""Pydantic models for the context window management subsystem.

Defines the core data shapes: ContextWindowSettings for per-node
configuration, CompactionResult for compaction output, and
CompactionState for tracking compaction history.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# Re-exported as zeroth.core.context_window API: the authored per-node settings
# are graph contract vocabulary; the compaction result objects stay here.
from zeroth.contracts.graph.models import ContextWindowSettings as ContextWindowSettings


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

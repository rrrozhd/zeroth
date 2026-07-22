"""Context window management for the Zeroth platform.

Provides token tracking, compaction threshold detection, and built-in
compaction strategies for managing LLM context window limits.
"""

from __future__ import annotations

from zeroth.runtime.context.errors import (
    CompactionError,
    ContextWindowError,
    TokenCountError,
)
from zeroth.runtime.context.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)
from zeroth.runtime.context.strategies import (
    CompactionStrategy,
    LLMSummarizationStrategy,
    ObservationMaskingStrategy,
    TruncationStrategy,
)
from zeroth.runtime.context.tracker import ContextWindowTracker

__all__ = [
    "CompactionError",
    "CompactionResult",
    "CompactionState",
    "CompactionStrategy",
    "ContextWindowError",
    "ContextWindowSettings",
    "ContextWindowTracker",
    "LLMSummarizationStrategy",
    "ObservationMaskingStrategy",
    "TokenCountError",
    "TruncationStrategy",
]

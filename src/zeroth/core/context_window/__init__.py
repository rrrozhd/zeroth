"""Context window management for the Zeroth platform.

Provides token tracking, compaction threshold detection, and built-in
compaction strategies for managing LLM context window limits.
"""

from __future__ import annotations

from zeroth.core.context_window.errors import (
    CompactionError,
    ContextWindowError,
    TokenCountError,
)
from zeroth.core.context_window.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)
from zeroth.core.context_window.strategies import (
    CompactionStrategy,
    LLMSummarizationStrategy,
    ObservationMaskingStrategy,
    TruncationStrategy,
)
from zeroth.core.context_window.tracker import ContextWindowTracker

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

"""Legacy import path for the runtime context package.

Context window management lives in :mod:`zeroth.runtime.context`; this
package republishes the same objects for compatibility. Import from the
canonical location instead (see docs/backend-import-migration.md).
"""

from zeroth.runtime.context import (
    CompactionError,
    CompactionResult,
    CompactionState,
    CompactionStrategy,
    ContextWindowError,
    ContextWindowSettings,
    ContextWindowTracker,
    LLMSummarizationStrategy,
    ObservationMaskingStrategy,
    TokenCountError,
    TruncationStrategy,
)

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

"""Error types for the context window management subsystem.

These exceptions are raised when something goes wrong during token
counting or message compaction. They all inherit from ContextWindowError,
so you can catch that one base class to handle any context-window-related
error.
"""

from __future__ import annotations


class ContextWindowError(Exception):
    """Base error for anything that goes wrong in the context window subsystem.

    Catch this if you want to handle all context-window-related errors
    in one place. More specific errors below inherit from this one.
    """


class CompactionError(ContextWindowError):
    """Raised when a compaction strategy fails to compact messages.

    This covers failures in any of the built-in strategies (truncation,
    observation masking, LLM summarization) or custom strategy implementations.
    """


class TokenCountError(ContextWindowError):
    """Raised when token counting fails.

    This wraps errors from litellm.token_counter so that callers can handle
    counting failures without depending on litellm's exception types directly.
    """

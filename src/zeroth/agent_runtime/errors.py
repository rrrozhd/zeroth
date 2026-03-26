"""Error classes for the agent runtime.

Each error here represents a specific kind of failure that can happen
when an agent runs. They all inherit from AgentRuntimeError, so you
can catch that single base class if you want to handle any agent error.
"""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base error for all agent runtime failures.

    Catch this if you want to handle any error from the agent runtime
    in one place. All other errors in this module inherit from this.
    """


class AgentInputValidationError(AgentRuntimeError):
    """Raised when the data you pass into an agent does not match the expected format."""


class AgentProviderError(AgentRuntimeError):
    """Raised when the AI model provider fails to produce a response."""


# Inherits from AgentProviderError (not AgentRuntimeError) because
# a timeout is a specific type of provider failure
class AgentTimeoutError(AgentProviderError):
    """Raised when the AI model takes too long to respond."""


class AgentOutputValidationError(AgentRuntimeError):
    """Raised when the AI model's response does not match the expected output format."""


class AgentRetryExhaustedError(AgentRuntimeError):
    """Raised when the agent has retried the maximum number of times and still failed."""

    def __init__(self, *, attempts: int, last_error: Exception):
        super().__init__(f"agent runtime exhausted retries after {attempts} attempt(s)")
        self.attempts = attempts
        # Keep the last error so callers can inspect what ultimately went wrong
        self.last_error = last_error

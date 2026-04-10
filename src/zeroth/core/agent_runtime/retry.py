"""Retry utilities for provider calls.

Classifies LLM provider errors as retryable (transient) or permanent,
and computes exponential backoff delays with jitter.
"""

from __future__ import annotations

import random

# Lazy import to avoid hard dependency on litellm at module level
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def is_retryable_provider_error(exc: BaseException) -> bool:
    """Classify whether a provider error is transient and worth retrying.

    Retryable: rate limits (429), server errors (500, 502, 503, 504), timeouts (408),
    connection errors.
    Permanent: authentication (401), bad request (400), permission denied (403),
    not found (404), context window exceeded, content policy violation.
    """
    try:
        import litellm
    except ImportError:
        return False

    if isinstance(exc, litellm.RateLimitError):
        return True
    if isinstance(exc, litellm.ServiceUnavailableError):
        return True
    if isinstance(exc, litellm.InternalServerError):
        return True
    if isinstance(exc, litellm.Timeout):
        return True
    if isinstance(exc, litellm.APIConnectionError):
        return True
    # Check for status_code attribute as fallback
    status_code = getattr(exc, "status_code", None)
    return status_code is not None and status_code in RETRYABLE_STATUS_CODES


def compute_backoff_delay(
    attempt: int,
    *,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
) -> float:
    """Compute exponential backoff delay with optional jitter.

    Args:
        attempt: The current attempt number (1-based; first retry is attempt 2).
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay cap in seconds.
        jitter: If True, add random jitter (full jitter strategy).

    Returns:
        Delay in seconds to wait before the next attempt.
    """
    # Exponential: base_delay * 2^(attempt-2) for attempt >= 2
    # attempt 2 -> base_delay * 1, attempt 3 -> base_delay * 2, etc.
    retry_number = max(attempt - 1, 0)
    delay = base_delay * (2 ** (retry_number - 1)) if retry_number > 0 else 0.0
    delay = min(delay, max_delay)
    if jitter and delay > 0:
        # Full jitter: uniform random between 0 and computed delay
        delay = random.uniform(0, delay)  # noqa: S311
    return delay

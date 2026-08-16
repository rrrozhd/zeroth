"""Configuration model for operational guardrails."""

from __future__ import annotations

from math import isfinite

from pydantic import BaseModel, ConfigDict, field_validator

MAX_RATE_LIMIT_RETRY_AFTER_SECONDS = 86_400
MIN_RATE_LIMIT_REFILL_RATE = 1 / MAX_RATE_LIMIT_RETRY_AFTER_SECONDS


class GuardrailConfig(BaseModel):
    """Tunable guardrail parameters for a deployment."""

    model_config = ConfigDict(extra="forbid")

    # Token-bucket rate limiting.
    rate_limit_capacity: float = 10.0
    rate_limit_refill_rate: float = 1.0

    # Daily quota (None = unlimited).
    quota_daily_limit: int | None = None

    # Dead-letter threshold: mark a run dead after this many consecutive failures.
    max_failure_count: int = 3

    # Backpressure: reject new runs when more than this many PENDING runs exist.
    backpressure_queue_depth: int = 100

    # Max concurrency for the durable worker.
    max_concurrency: int = 8

    @field_validator("rate_limit_refill_rate")
    @classmethod
    def _validate_refill_rate(cls, value: float) -> float:
        if not isfinite(value) or not MIN_RATE_LIMIT_REFILL_RATE <= value <= 100_000:
            raise ValueError("rate_limit_refill_rate must provide 1 token/day to 100,000/second")
        return value

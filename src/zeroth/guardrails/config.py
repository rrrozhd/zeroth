"""Configuration model for operational guardrails."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GuardrailConfig(BaseModel):
    """Tunable guardrail parameters for a deployment."""

    model_config = ConfigDict(extra="forbid")

    # Token-bucket rate limiting.
    rate_limit_capacity: float = 10.0
    rate_limit_refill_rate: float = 1.0  # tokens per second

    # Daily quota (None = unlimited).
    quota_daily_limit: int | None = None

    # Dead-letter threshold: mark a run dead after this many consecutive failures.
    max_failure_count: int = 3

    # Backpressure: reject new runs when more than this many PENDING runs exist.
    backpressure_queue_depth: int = 100

    # Max concurrency for the durable worker.
    max_concurrency: int = 8

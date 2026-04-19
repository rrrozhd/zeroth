# Usage Guide: Guardrails

## Overview

This guide shows how to configure and exercise Zeroth's runtime guardrails: `TokenBucketRateLimiter`, `QuotaEnforcer`, and the `DeadLetterManager`. All three are plain async classes backed by the same `AsyncDatabase` the rest of Zeroth uses, so they slot into a deployment without extra infrastructure.

See [Concept: guardrails](../concepts/guardrails.md) for the model; see [Concept: policy](../concepts/policy.md) for the compile-time counterpart.

## Minimal example

```python
from zeroth.core.guardrails import (
    GuardrailConfig,
    QuotaEnforcer,
    TokenBucketRateLimiter,
)

# 1. Declare the tunables for this deployment.
config = GuardrailConfig(
    rate_limit_capacity=20.0,
    rate_limit_refill_rate=2.0,       # tokens per second
    quota_daily_limit=10_000,
    max_failure_count=5,
    backpressure_queue_depth=200,
    max_concurrency=8,
)

# 2. Build the enforcers on top of the shared AsyncDatabase.
rate_limiter = TokenBucketRateLimiter(database=database)
quota = QuotaEnforcer(database=database)

# 3. Check on each incoming run request.
bucket_key = f"tenant:{principal.tenant_id}:deployment:{deployment.deployment_ref}"
allowed = await rate_limiter.check_and_consume(
    bucket_key,
    capacity=config.rate_limit_capacity,
    refill_rate=config.rate_limit_refill_rate,
)
if not allowed:
    raise HTTPException(status_code=429, detail="rate_limited")

if config.quota_daily_limit is not None:
    within_quota = await quota.check_and_increment(
        f"tenant:{principal.tenant_id}:daily",
        limit=config.quota_daily_limit,
    )
    if not within_quota:
        raise HTTPException(status_code=429, detail="quota_exhausted")
```

That's the complete guardrail check: a run is only handed to the orchestrator if both the bucket has tokens and the daily quota is not yet exhausted.

## Common patterns

- **Per-tenant rate limits.** Use `tenant_id` as the bucket-key prefix to isolate tenants from each other's bursts.
- **Per-deployment concurrency caps.** Combine `max_concurrency` with the durable worker's pool size so a single deployment cannot monopolize the worker.
- **Sliding daily budget.** `QuotaEnforcer` with `window_seconds=86400` gives you a rolling 24-hour window that resets automatically after the window expires.
- **Dead-letter parking.** Wire `DeadLetterManager` to the durable worker so runs hitting `max_failure_count` consecutive failures are parked rather than retried forever.

## Pitfalls

1. **Shared bucket keys.** Using a single `bucket_key="global"` makes every tenant compete for the same tokens — always scope by tenant / deployment / caller.
2. **Capacity vs refill mismatch.** A capacity of `10` with a refill rate of `0.1/s` refills fully only once every 100 seconds; make sure the refill rate matches expected traffic or you'll rate-limit yourself.
3. **Forgetting backpressure.** Guardrails block *new* work; if you don't drain the durable queue, you still build up an unbounded backlog of pending runs.
4. **Counting failures on success.** Be careful that the `DeadLetterManager` increment runs only on terminal failure, not on transient node retries — otherwise healthy runs get parked.
5. **Guardrails are not a substitute for policy.** A guardrail only caps *volume*; it does not decide *whether a specific action is legal*. Pair guardrails with [policy](policy.md) for defense in depth.

## Reference cross-link

See the [Python API reference for `zeroth.core.guardrails`](../reference/python-api/guardrails.md).

Related: [Concept: guardrails](../concepts/guardrails.md), [Concept: policy](../concepts/policy.md), [Concept: dispatch](../concepts/dispatch.md), [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md).

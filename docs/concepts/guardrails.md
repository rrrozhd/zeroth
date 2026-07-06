# Guardrails

## What it is

**Guardrails** are the operational safety net that sits between a deployment and the outside world: token-bucket rate limiting, rolling-window quotas, dead-letter thresholds, backpressure, and concurrency caps.

Where [policy](policy.md) answers *"is this node allowed to run?"*, guardrails answer *"is it safe to accept more work right now?"*. Guardrails run every time a deployment accepts a new run — before the orchestrator even touches the graph.

They are the last line of defense that does *not* require reading any agent logic to activate.

## Why it exists

Left unattended, a multi-agent system can quietly melt: a noisy tenant floods the queue, a buggy node retries itself forever, a cost runaway drains a budget in hours.

Guardrails are the blunt, fast-path defenses that keep the service operable even when upstream logic goes wrong. They are intentionally coarse (per-key counters, not per-call LLM judges) so the enforcement cost is negligible, and they are intentionally *runtime*: they protect the system in the moments between "request accepted" and "node executed", where the [policy](policy.md) pre-flight can't reach.

Because every guardrail is backed by the same `AsyncDatabase` the rest of Zeroth uses, counters survive restarts and are shared across worker replicas — there is no in-memory drift to worry about.

## Where it fits

Guardrails are layered atop the [dispatch](dispatch.md) path and the durable worker. When a run is enqueued, the service checks a `TokenBucketRateLimiter` for the caller's bucket key, then a `QuotaEnforcer` for the tenant's daily counter. If either returns `False`, the run is rejected before the [orchestrator](orchestrator.md) ever sees it.

Once the run *is* accepted, the `DeadLetterManager` watches consecutive failures: more than `max_failure_count` in a row and the run is marked dead, preventing retry storms. `backpressure_queue_depth` and `max_concurrency` on `GuardrailConfig` provide the final outer limits.

Guardrails complement [policy](policy.md) (which blocks *illegal* work) by bounding *legal but excessive* work, and they feed the [audit](audit.md) trail when they trip.

## Key types

- **`GuardrailConfig`** — the tunable knob set: `rate_limit_capacity`, `rate_limit_refill_rate`, `quota_daily_limit`, `max_failure_count`, `backpressure_queue_depth`, `max_concurrency`.
- **`TokenBucketRateLimiter`** — async-database-backed per-key token bucket; `check_and_consume(bucket_key, capacity=..., refill_rate=...)` returns `bool`.
- **`QuotaEnforcer`** — rolling-window counter; `check_and_increment(counter_key, limit=..., window_seconds=...)` returns `bool`.
- **`DeadLetterManager`** — tracks consecutive failure counts per run and parks runs that exceed the threshold.

## See also

- [Usage Guide: guardrails](../how-to/guardrails.md) — attach a rate limiter and quota to a deployment.
- [Concept: policy](policy.md) — the compile-time counterpart to runtime guardrails.
- [Concept: dispatch](dispatch.md) — where the rate-limit and quota checks actually fire.
- [Concept: audit](audit.md) — guardrail trips are recorded alongside run events.
- [Concept: econ](econ.md) — quota enforcement pairs with cost budgets from the economics layer.
- [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md) — end-to-end policy + approval + audit story.

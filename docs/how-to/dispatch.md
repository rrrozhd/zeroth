# Using dispatch

## Overview

You rarely construct a `RunWorker` by hand — `bootstrap_service` does that
for every deployment. But when you scale out, turn on low-latency
wakeups, or tune concurrency, you are working directly with this
subsystem. This page shows how to enable the optional Redis/arq wakeup
channel, how to shape the poll loop for your workload, and what to watch
out for when dispatch is the bottleneck.

## Install

Redis + arq wakeup is gated behind an extra:

```bash
pip install 'zeroth-core[dispatch]'
# or with uv
uv add 'zeroth-core[dispatch]'
```

This pulls `redis>=5.0.0` and `arq>=0.27`. Without it, dispatch still
works — it just polls instead of being poked.

## Minimal example

```python
from zeroth.core.dispatch import LeaseManager, RunWorker
from zeroth.core.dispatch import create_arq_pool, enqueue_wakeup  # [dispatch] extra

# LeaseManager and RunWorker are normally constructed inside bootstrap_service.
worker = RunWorker(
    deployment_ref="default",
    run_repository=run_repo,
    orchestrator=orchestrator,
    graph=graph,
    lease_manager=LeaseManager(run_repo),
    max_concurrency=16,   # bump from the default 8
    poll_interval=0.5,
)

await worker.start()          # reclaims orphaned RUNNING runs, then polls
# ...driven by the service lifespan; no manual poll_loop in production...

# Optional: push a wakeup when you know a run just became PENDING
arq_pool = await create_arq_pool(settings.redis)
if arq_pool is not None:
    await enqueue_wakeup(arq_pool, run_id="run-abc")
```

## Common patterns

- **Worker scale-out** — Run N service processes against the same
  storage. Leases guarantee at-most-one execution per run.
- **Concurrency tuning** — Raise `max_concurrency` when the bottleneck
  is LLM latency; lower it when you need backpressure.
- **Wakeups only** — The arq channel is a *notification*, never the
  queue. Even if Redis disappears, runs still execute on the poll
  interval.
- **Graceful shutdown** — Send SIGTERM; the lifespan hook cancels the
  poll loop and lets in-flight runs drain up to `shutdown_timeout`.

## Pitfalls

1. **Redis assumed authoritative** — It is not. Postgres lease store
   is the truth. Treat `create_arq_pool` returning `None` as normal.
2. **Visibility timeouts** — Leases expire; if a worker pauses longer
   than the lease TTL, another worker will reclaim the run. Tune TTL
   above your worst-case step duration.
3. **At-least-once** — If a crash happens between "step done" and
   "lease released", the next worker may re-execute the last step.
   Idempotency at the node level is non-negotiable.
4. **Oversized concurrency** — Each slot holds an LLM client, a DB
   connection, and an orchestrator frame. `max_concurrency=200` on a
   single worker will OOM.
5. **Forgetting the extra** — Importing `enqueue_wakeup` without
   `zeroth-core[dispatch]` silently falls through to poll-only mode.

## Reference cross-link

API reference for `zeroth.core.dispatch` will live under the Reference
quadrant (Phase 32). Related guides:
[concepts/dispatch](../concepts/dispatch.md) ·
[service how-to](service.md) · [webhooks how-to](webhooks.md).

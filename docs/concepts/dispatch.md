# Dispatch

## What it is

The `zeroth.core.dispatch` subsystem is Zeroth's **durable run dispatcher**:
a long-lived worker that claims PENDING runs from storage, drives them
through the orchestrator to completion, and releases its lease — with an
optional Redis/arq wakeup channel for low-latency scheduling. It is the
replacement for naive `asyncio.create_task` dispatch.

## Why it exists

A production platform cannot lose runs when a process crashes. Fire-and-
forget asyncio tasks die with their parent and leave the run store in an
inconsistent "RUNNING forever" state. Dispatch solves that by making
the storage layer the authoritative queue: workers claim runs via
lease, orphaned leases are reclaimed on startup, and every state
transition is persisted. Optional Redis-backed wakeup (arq) sits on top
as a *notification* channel only — Postgres still owns the truth.

## Where it fits

Dispatch is owned by the [service](service.md) lifespan: `create_app`
starts a `RunWorker` inside `bootstrap_service`, and the worker drives
the [orchestrator](orchestrator.md) against the deployment's
[graph](graph.md) for every PENDING [run](runs.md). When wakeup is
enabled, the `[dispatch]` extra pulls in `redis` + `arq` and
`enqueue_wakeup` fires a best-effort signal to kick the poll loop
immediately instead of waiting for the next tick.

## Key types

- **`RunWorker`** — Long-lived dataclass worker. Owns `start()`,
  `poll_loop()`, and graceful `stop()`. Bounded concurrency via an
  asyncio semaphore (default 8).
- **`LeaseManager`** — Storage-backed lease lifecycle: acquire, renew,
  release, reclaim expired.
- **`WAKEUP_TASK_NAME`** / `enqueue_wakeup()` — arq task name and
  best-effort producer for the wakeup channel.
- **`create_arq_pool()`** / **`arq_settings_from_zeroth()`** — Helpers
  to build an arq `RedisSettings` from Zeroth's config (only available
  when the `[dispatch]` extra is installed).
- **`run_arq_consumer()`** — Convenience for running an arq worker that
  listens for wakeups and forwards them to the local `RunWorker`.

## See also

- Usage Guide: [how-to/dispatch](../how-to/dispatch.md)
- Related: [orchestrator](orchestrator.md), [runs](runs.md),
  [service](service.md)

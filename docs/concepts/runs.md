# Runs

## What it is

A **run** is a single execution of a Zeroth graph, persisted as a database row with a full history of every node that executed, every condition that was evaluated, and every state transition the orchestrator made. A **thread** groups related runs together — for example, the turns in a single conversation — so you can track an ongoing task across multiple executions.

## Why it exists

Agent systems are long-lived, partially-async, and often pause for approvals or external input. You can't treat an execution as "a function call that either returned or raised" — you need a durable record of where it is, what it has done, and how it got there.

Runs give you that record. Every run has an explicit lifecycle (PENDING → RUNNING → WAITING_APPROVAL → COMPLETED / FAILED) with the valid transitions enforced at the repository layer.

That means operators can replay, resume, debug, and audit any execution long after the original request is gone, and the governance plane can reason about what each run is allowed to do next.

## Where it fits

The [orchestrator](orchestrator.md) produces and advances runs — every node the orchestrator picks up appends a `RunHistoryEntry` and may trigger a status transition. Runs are persisted through [storage](storage.md) via `RunRepository` and `ThreadRepository`, both of which use the shared `AsyncDatabase` protocol. Approvals, audit, and economics all hang off of the run as their correlation key — making runs the canonical handle by which everything else in Zeroth refers to "this particular execution".

## Key types

All of these live under `zeroth.core.runs`:

- **`Run`** — the full Pydantic model of a single execution, including its status, thread ID, and history.
- **`RunStatus`** — the enum of lifecycle states (re-exported from GovernAI).
- **`RunHistoryEntry`** — one node execution: node ID, status, input/output snapshots, timing.
- **`RunConditionResult`** — a recorded conditional-edge decision.
- **`RunFailureState`** — structured failure detail when a run fails.
- **`Thread` / `ThreadStatus` / `ThreadMemoryBinding`** — the group-of-runs abstraction and its lifecycle.
- **`RunRepository` / `ThreadRepository`** — async repositories that read and write runs and threads with valid-transition enforcement.

The repositories encode an `ALLOWED_TRANSITIONS` table — for example, a run cannot go directly from `PENDING` to `COMPLETED`, and `COMPLETED` is terminal. This is what lets operator tooling safely reason about whether a given run can be replayed, resumed, or archived without loading business logic.

The separation between `Run` and `Thread` is deliberate: a `Run` is a single attempt, while a `Thread` is an ongoing conversation or task that owns many runs and carries the long-lived memory bindings across them. Resume logic lives on threads, replay logic lives on runs.

## See also

- [Usage Guide: runs](../how-to/runs.md) — execute a graph and inspect its run state.
- [Concept: orchestrator](orchestrator.md) — the producer of runs.
- [Concept: storage](storage.md) — where runs are persisted.
- [Usage Guide: orchestrator](../how-to/orchestrator.md) — build and execute a graph that produces runs.

# How to work with runs

## Overview

A run is Zeroth's persistent record of one graph execution. After the orchestrator finishes (or pauses) a run, you can load it back from the repository to inspect its status, history, outputs, and failure state. This guide shows how to execute a graph and read back the resulting run.

## Minimal example

```python
import asyncio

from zeroth.core.runs import Run, RunRepository, RunStatus
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


async def main() -> None:
    # 1. Open a database and a run repository
    db = AsyncSQLiteDatabase(path=":memory:")
    await db.connect()
    repo = RunRepository(db)
    await repo.initialize()

    # 2. Create a run (in practice, the orchestrator does this for you)
    run = Run(
        graph_id="doc.summariser",
        thread_id=None,
        status=RunStatus.PENDING,
        inputs={"text": "Hello world"},
    )
    await repo.create(run)

    # 3. ...orchestrator executes the graph, advancing the run...
    await repo.transition(run.id, RunStatus.RUNNING)
    await repo.transition(run.id, RunStatus.COMPLETED)

    # 4. Load it back and inspect its state
    loaded: Run = await repo.get(run.id)
    print("status :", loaded.status)
    print("outputs:", loaded.outputs)
    print("history:")
    for entry in loaded.history:
        print(f"  {entry.node_id} -> {entry.status}  (attempt {entry.attempt})")


asyncio.run(main())
```

After any graph execution, loading a `Run` by its ID gives you everything the orchestrator wrote: the final `RunStatus`, every `RunHistoryEntry`, every `RunConditionResult`, and — if the run failed — a structured `RunFailureState` explaining why.

## Common patterns

- **Filter by thread.** Use `ThreadRepository.list_runs(thread_id)` to get every run that belongs to one conversation or long-lived task.
- **Check status before acting.** Operator tooling should always check `run.status` before attempting a replay — `RunStatus.COMPLETED` is terminal, `RunStatus.FAILED` can be replayed, others are in-flight.
- **Inspect the last entry.** `run.history[-1]` is the most recent node that executed and is usually the best place to start when debugging a partial run.
- **Use `ThreadMemoryBinding`.** When resuming a thread, load its `ThreadMemoryBinding` to connect the new run to the thread's persistent memory.

## Pitfalls

1. **Invalid transitions.** `RunRepository.transition` enforces `ALLOWED_TRANSITIONS`. You cannot jump from `PENDING` directly to `COMPLETED`; attempting to do so raises an error. Always move through `RUNNING`.
2. **Mutating loaded runs.** A `Run` returned from `repo.get` is a snapshot — mutating it in Python does not update the database. Use `repo.update` or `repo.transition` to persist changes.
3. **Thread vs. run scope.** Memory and approval context belong on threads, not runs. Attaching long-lived state to a single run loses it on the next turn.
4. **Assuming `outputs` is always populated.** Outputs are only set once the orchestrator marks the run `COMPLETED`. For failed or in-flight runs, check `run.failure_state` and `run.history` instead.
5. **Forgetting initialisation.** `RunRepository` and `ThreadRepository` require `initialize()` to be called once before first use (they run Alembic migrations at startup).

## Reference cross-link

See the [Python API reference for `zeroth.core.runs`](../reference/python-api/runs.md).

# Attach memory to an agent

## What this recipe does
Registers a `RunEphemeralMemoryConnector` under a memory ref and
writes/reads a value scoped to a single run, so an agent can keep
scratch state across turns without a database.

## When to use
- An agent needs a notepad that survives across multiple turns inside
  one run (e.g. partial plans, running totals, tool call history).
- You want a fast in-process connector for tests and examples.
- You're prototyping a workflow and don't want to spin up Postgres or
  Chroma yet — you will swap in a real connector later.

## When NOT to use
- The data must survive the run — use `ThreadMemoryConnector` or a
  persistent backend (`memory-pg`, `memory-chroma`, `memory-es`).
- Multiple agents across processes need shared state — the ephemeral
  connector is in-process only.

## Recipe
```python
--8<-- "attach_memory.py"
```

## How it works
`InMemoryConnectorRegistry.register` stores a `(manifest, connector)`
tuple under a memory ref. At run time the orchestrator resolves the ref
to the live connector and wraps it with `ScopedMemoryConnector` so
`MemoryScope.RUN` writes automatically target the current `run_id`.
The ephemeral connector keeps everything in a process-local dict —
zero infrastructure, zero I/O.

## See also
- [Usage Guide: memory](../memory.md)
- [Concept: memory](../../concepts/memory.md)
- [Concept: agents](../../concepts/agents.md)

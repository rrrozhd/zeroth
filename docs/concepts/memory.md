# Memory

## What it is

The **memory** subsystem gives Zeroth agents a place to read and write information that outlives a single function call — scratch space for the current run, conversation history for a thread, shared key-value data across runs, or long-term embeddings for retrieval. Memory is accessed through **connectors**, each of which implements the GovernAI `MemoryConnector` protocol and is registered under a name so that agents and nodes can look it up at runtime.

## Why it exists

Agents need somewhere to remember things, but "somewhere" is not one thing. Ephemeral scratch space in RAM, Redis-backed key/value stores, vector databases, and full-text search engines all serve legitimate memory use-cases — and every production deployment ends up needing some mix of them.

Memory solves this by making the *interface* uniform (read, write, delete, scope, target) while letting the *backing store* vary. An agent asks the registry for "the memory bound to my thread" and gets back a connector; it doesn't know or care whether that connector is writing to a dict, to Postgres, to Chroma, or to Elasticsearch.

The three optional flavours — pgvector, chroma, elasticsearch — are packaged as installable extras (`memory-pg`, `memory-chroma`, `memory-es`) so a base install of `zeroth-core` stays dependency-light.

## Where it fits

Memory connectors persist their data through the [storage](storage.md) layer — `pgvector` uses the Postgres backend, the in-process connectors use plain dicts, Chroma and Elasticsearch use their own HTTP clients. [Agents](agents.md) consume memory via bindings registered on their thread: the orchestrator resolves those bindings at run start and injects the concrete connector into the agent's execution context. This is how a long-lived thread carries context across many runs without any node having to know which physical store is behind it.

## Key types

All of these live under `zeroth.core.memory`:

- **`RunEphemeralMemoryConnector`** — in-process, per-run scratch memory that disappears when the run ends.
- **`KeyValueMemoryConnector`** — shared key-value memory, typically Redis-backed.
- **`ThreadMemoryConnector`** — thread-scoped memory that persists across all runs in a thread.
- **`ConnectorManifest`** — declarative description of a connector (type, config, capabilities).
- **`ResolvedMemoryBinding`** — a connector resolved and ready to inject into an agent.
- **`InMemoryConnectorRegistry` / `MemoryConnectorResolver`** — register connectors by name and look them up.
- **`register_memory_connectors`** — factory helper that registers the built-in connector set.
- **`PgVectorMemoryConnector`** *(extra: `memory-pg`)*, **`ChromaMemoryConnector`** *(extra: `memory-chroma`)*, **`ElasticsearchMemoryConnector`** *(extra: `memory-es`)* — the three optional vector/search backends.

Scopes (`MemoryScope.RUN`, `THREAD`, `GLOBAL`) and the `target` argument together give every write a deterministic coordinate — so two callers writing the same `key` into different scopes or targets never collide, and the governance plane can reason about what data a tool is allowed to read.

## Picking a backend

A useful rule of thumb: if you already run Postgres, use `memory-pg` — one fewer service to operate. If you need a vector store that can be embedded in-process, use `memory-chroma`. If you already have an Elasticsearch fleet and want BM25 alongside vectors, use `memory-es`. You can mix several connectors in one deployment.

## See also

- [Usage Guide: memory](../how-to/memory.md) — wire a memory connector to an agent and pick an extra.
- [Concept: storage](storage.md) — where memory connectors persist their data.
- [Concept: agents](agents.md) — agents consume memory via thread bindings.

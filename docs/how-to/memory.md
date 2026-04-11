# How to use memory

## Overview

Memory gives a Zeroth agent somewhere to read and write information that outlives a single function call. You wire up a memory connector, register it under a name, and bind it to an agent's thread; at run time, the orchestrator resolves the binding and hands the agent a ready-to-use connector. This guide shows how to use the always-available in-process connectors, and how to install and enable the three optional vector/search backends.

## Minimal example

This example uses only the in-process `RunEphemeralMemoryConnector`, which requires no extras — it works out of the box with a plain `pip install zeroth-core`:

```python
import asyncio
from governai.memory.models import MemoryScope

from zeroth.core.memory import RunEphemeralMemoryConnector


async def main() -> None:
    memory = RunEphemeralMemoryConnector()

    # Write a value scoped to the current run
    await memory.write(
        key="last_user_message",
        value={"text": "Hello world"},
        scope=MemoryScope.RUN,
        target="run-42",
    )

    # Read it back
    entry = await memory.read(
        key="last_user_message",
        scope=MemoryScope.RUN,
        target="run-42",
    )
    print(entry.value if entry else None)


asyncio.run(main())
```

For a durable, multi-run memory, swap the connector for a `KeyValueMemoryConnector` (Redis-backed) or one of the three vector backends described below.

## Common patterns

- **Register connectors at startup.** Use `register_memory_connectors(registry)` once during bootstrap; downstream code then resolves by name via `MemoryConnectorResolver`, never by class.
- **Pick the right scope.** `MemoryScope.RUN` for scratch memory, `MemoryScope.THREAD` for conversation history, `MemoryScope.GLOBAL` for facts shared across threads.
- **Install only what you use.** The three vector/search connectors are optional extras — pick the one your ops team already runs in production rather than adding a new data store:

  ```bash
  pip install 'zeroth-core[memory-pg]'      # Postgres + pgvector
  pip install 'zeroth-core[memory-chroma]'  # ChromaDB
  pip install 'zeroth-core[memory-es]'      # Elasticsearch 8.x
  ```

  Each extra pulls in the corresponding driver (`psycopg`, `chromadb-client`, `elasticsearch[async]`). Importing the matching connector class before installing its extra raises `ImportError` — this is deliberate so that base installs stay dependency-light.

- **Use `ConnectorManifest` for configuration.** Describe each connector declaratively (type, DSN, collection name) and load manifests from config, not code.

## Pitfalls

1. **Scope confusion.** Writing with `MemoryScope.RUN` and reading with `MemoryScope.THREAD` silently returns `None`. Always use the same scope on both sides.
2. **Forgetting `target`.** The `target` argument partitions memory within a scope (e.g. `run-42`). Omitting it is not an error, but two callers with no `target` share the same bucket.
3. **Installing the wrong extra.** `memory-pg` requires a Postgres server *and* the `pgvector` extension. `memory-chroma` needs a running Chroma service (or an in-process client). `memory-es` needs Elasticsearch 8.x — 7.x is not supported.
4. **Mixing connectors by accident.** Don't register two connectors under the same name; the second registration wins silently and the first is dropped.
5. **Assuming persistence.** `RunEphemeralMemoryConnector` throws everything away when the run ends. If you need to read it back in a later run, use a thread or key-value connector instead.

## Reference cross-link

See the [Python API reference for `zeroth.core.memory`](../reference/python-api/memory.md).

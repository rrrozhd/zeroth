"""Attach memory to an agent — runnable example for docs/how-to/cookbook/attach-memory.md.

Uses the in-process RunEphemeralMemoryConnector to show how an agent node
can read and write scratch state scoped to a single run, and how a
ConnectorManifest plus InMemoryConnectorRegistry is wired so the
orchestrator can resolve bindings at run start. No external services
required — the ephemeral connector keeps everything in memory.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def _run_demo() -> int:
    from governai.memory.models import MemoryScope

    from zeroth.core.memory import (
        ConnectorManifest,
        InMemoryConnectorRegistry,
        RunEphemeralMemoryConnector,
    )

    # 1. Register an ephemeral connector under a stable reference so the
    #    orchestrator can look it up by name when an agent node asks for it.
    connector = RunEphemeralMemoryConnector()
    manifest = ConnectorManifest(
        connector_type="ephemeral",
        scope=MemoryScope.RUN,
    )
    registry = InMemoryConnectorRegistry()
    registry.register("memory://demo-scratch", manifest, connector)

    # 2. The orchestrator would normally resolve the ref on your behalf;
    #    here we do it directly to show the raw connector surface.
    _, raw = registry.resolve("memory://demo-scratch")

    # 3. Write a note scoped to a run, then read it back on a later turn.
    run_id = "run-demo-001"
    await raw.write(
        key="greeting",
        value={"text": "hello"},
        scope=MemoryScope.RUN,
        target=run_id,
    )
    entry = await raw.read(key="greeting", scope=MemoryScope.RUN, target=run_id)
    assert entry is not None, "expected to read back the value we just wrote"
    print(f"agent-memory read: key={entry.key} value={entry.value}")

    # 4. Search returns all entries whose key or value contains the query text.
    hits = await raw.search({"text": "hello"}, scope=MemoryScope.RUN, target=run_id)
    print(f"search hits: {len(hits)}")
    return 0


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    sys.exit(main())

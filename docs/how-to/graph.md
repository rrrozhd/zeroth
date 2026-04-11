# Graph: usage guide

## Overview

This guide shows how to build, persist, and publish a [graph](../concepts/graph.md) — the declarative workflow object that the orchestrator executes. A graph is a `Graph` Pydantic model containing `AgentNode`, `ExecutableUnitNode`, or `HumanApprovalNode` entries wired together with `Edge` objects. You author it in code (or load it from JSON), hand it to a `GraphRepository` for storage, and `publish()` it to make it runnable.

## Minimal example

```python
import asyncio

from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    GraphRepository,
)
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


async def main() -> None:
    database = AsyncSQLiteDatabase(path=":memory:")
    repo = GraphRepository(database)

    agent = AgentNode(
        node_id="greet",
        graph_version_ref="demo:1",
        agent=AgentNodeData(
            instruction="Say hello",
            model_provider="openai/gpt-4o-mini",
        ),
    )
    tool = ExecutableUnitNode(
        node_id="echo",
        graph_version_ref="demo:1",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://echo",
            execution_mode="native",
        ),
    )
    graph = Graph(
        graph_id="demo",
        name="demo-graph",
        entry_step="greet",
        nodes=[agent, tool],
        edges=[Edge(edge_id="e1", source_node_id="greet", target_node_id="echo")],
    )

    created = await repo.create(graph)
    await repo.publish(created.graph_id, created.version)


asyncio.run(main())
```

## Common patterns

- **Author-then-publish** — build a `Graph`, persist it as `DRAFT` via `repo.create()`, then call `repo.publish()` to flip it to `PUBLISHED`.
- **Compile to spec** — call `graph.to_governed_flow_spec()` when you need the `GovernedFlowSpec` form (e.g. for the orchestrator or external tooling).
- **Versioned evolution** — never mutate a published graph; create a new version in `DRAFT`, test, then publish. `GraphStatus` transitions are enforced by `transition_to()`.
- **Conditional branching** — attach a `Condition` to one or more `Edge` objects so the [conditions](../concepts/conditions.md) subsystem can route the run at that step.

## Pitfalls

1. **Forgetting `entry_step`** — if omitted, `to_governed_flow_spec()` defaults to the first node in declaration order; be explicit for readability and stability across refactors.
2. **Dangling edge references** — `Graph` runs a post-init validator that rejects edges pointing at unknown `node_id` values. Build your node list first, then your edges.
3. **Editing a published graph in place** — `transition_to()` forbids `PUBLISHED -> DRAFT`. Always fork a new version to iterate.
4. **Missing contract refs on agents** — agent nodes without `input_contract_ref`/`output_contract_ref` will run, but the orchestrator cannot enforce schema validation on their I/O.
5. **Putting `max_visits_per_node` too low** — cycles legitimately revisit nodes; pick a realistic ceiling in `ExecutionSettings` rather than the default of 10 if your graph loops.

## Reference cross-link

See the [Python API reference for `zeroth.core.graph`](../reference/python-api.md#zerothcoregraph) (generated in Phase 32).

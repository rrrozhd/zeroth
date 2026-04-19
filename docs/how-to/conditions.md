# Conditions: usage guide

## Overview

This guide shows how to attach a condition to a graph edge so the [orchestrator](../concepts/orchestrator.md) branches at run time. The subsystem is described in the [conditions concept page](../concepts/conditions.md); it turns a `Condition` on an `Edge` into an actual routing decision via `NextStepPlanner`, `ConditionEvaluator`, and `BranchResolver`, and records the outcome on the audit trail. You author a condition declaratively on the graph; the orchestrator does the rest.

## Minimal example

```python
import asyncio

from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    Graph,
    GraphRepository,
)
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


async def main() -> None:
    classifier = AgentNode(
        node_id="classify",
        graph_version_ref="demo:1",
        agent=AgentNodeData(instruction="Classify intent", model_provider="openai/gpt-4o-mini"),
    )
    refund = AgentNode(
        node_id="refund",
        graph_version_ref="demo:1",
        agent=AgentNodeData(instruction="Issue refund", model_provider="openai/gpt-4o-mini"),
    )
    escalate = AgentNode(
        node_id="escalate",
        graph_version_ref="demo:1",
        agent=AgentNodeData(instruction="Escalate to human", model_provider="openai/gpt-4o-mini"),
    )

    refund_edge = Edge(
        edge_id="to_refund",
        source_node_id="classify",
        target_node_id="refund",
        condition=Condition(expression="output.intent == 'refund'"),
    )
    escalate_edge = Edge(
        edge_id="to_escalate",
        source_node_id="classify",
        target_node_id="escalate",
        condition=Condition(expression="output.intent != 'refund'"),
    )

    graph = Graph(
        graph_id="demo",
        name="branching-demo",
        entry_step="classify",
        nodes=[classifier, refund, escalate],
        edges=[refund_edge, escalate_edge],
    )

    repo = GraphRepository(AsyncSQLiteDatabase(path=":memory:"))
    created = await repo.create(graph)
    await repo.publish(created.graph_id, created.version)


asyncio.run(main())
```

## Common patterns

- **Single-expression branch** — default `branch_rule="expression"`; one edge evaluates to true, the orchestrator follows it.
- **Any-of branch** — set `branch_rule="any"` to take the first true edge in declaration order (useful for priority routing).
- **All-of fan-out** — set `branch_rule="all"` to traverse every edge whose condition holds, fanning the run out to parallel branches.
- **Cycle-safe loops** — set `allow_cycle_traversal=True` on the `Condition` to let an edge revisit an already-visited node (guarded by `ExecutionSettings.max_visits_per_node`).

## Pitfalls

1. **Conditions that reference undefined operands** — list every variable the expression reads in `operand_refs`; the binder will fail fast if the context can't supply them.
2. **Silent fall-through** — if no condition on any outgoing edge is true, the orchestrator has no next step; always include a default/else edge.
3. **Expressions that mutate state** — evaluators should be pure; any side effect belongs in a node, not in a condition.
4. **Forgetting `max_visits_per_node`** — cycle-traversal conditions can loop forever without the visit cap; don't disable it.
5. **Branching on unvalidated agent output** — if the condition reads `output.intent` but the agent output contract does not guarantee that field, you will see intermittent routing errors; validate first.

## Reference cross-link

See the [Python API reference for `zeroth.core.conditions`](../reference/python-api/conditions.md).

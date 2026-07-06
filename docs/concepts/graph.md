# Graph

## What it is

A **graph** is Zeroth's declarative description of a multi-agent workflow: a versioned collection of nodes (steps) and edges (connections) plus the execution settings that govern how it runs. It is the primary artifact you author when building on Zeroth.

## Why it exists

Multi-agent systems quickly collapse into tangles of ad-hoc function calls, implicit state, and hand-rolled control flow. The graph gives you a single, inspectable, versionable object that captures *what* happens, *in what order*, and *under what governance*, while deferring *how* each step runs to the [orchestrator](orchestrator.md). Because a `Graph` is a Pydantic model, it can be validated, diffed, stored, published, archived, and compiled to a `GovernedFlowSpec` for execution — without ever touching running code.

## Where it fits

The graph sits at the center of Zeroth. It is produced by your code (or by the Studio UI), persisted via `GraphRepository`, and handed to the [orchestrator](orchestrator.md) at run time. Nodes reference [agents](agents.md) and [execution units](execution-units.md); edges carry [conditions](conditions.md) that branch the run. Adjacent subsystems — contracts, policy, approvals, audit — attach to the graph through refs on nodes and edges, so the graph is also the bind site for governance.

## Key types

- **`Graph`** — top-level workflow object with nodes, edges, `ExecutionSettings`, lifecycle status, and a `to_governed_flow_spec()` compiler.
- **`Node`** — discriminated union of `AgentNode`, `ExecutableUnitNode`, and `HumanApprovalNode`; one per step.
- **`Edge`** — directed connection between two nodes, optionally carrying an `EdgeMapping` and a `Condition`.
- **`GraphStatus`** — lifecycle enum (`DRAFT`, `PUBLISHED`, `ARCHIVED`) enforced by `transition_to()`.
- **`GraphRepository`** — persistence layer that stores, versions, and retrieves graphs from a database.

## See also

- [Usage Guide: graph](../how-to/graph.md)
- [Concept: orchestrator](./orchestrator.md)
- [Concept: conditions](./conditions.md)

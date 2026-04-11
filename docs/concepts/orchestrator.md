# Orchestrator

## What it is

The **orchestrator** is Zeroth's execution engine: the runtime that takes a published [graph](graph.md) and drives it from entry node to terminal state, handling branching, approvals, policy checks, cost enforcement, and durable run state along the way.

## Why it exists

A graph on its own is inert data. Something has to walk it, invoke agents and executable units, evaluate conditions at each edge, pause for human approvals, enforce cost budgets, and persist progress so interrupted runs can resume. Baking that logic into individual nodes would duplicate it everywhere and make governance impossible. The `RuntimeOrchestrator` centralizes it in one place, so every node benefits from the same policy gates, audit trail, and failure semantics regardless of whether it runs an LLM agent, a sandboxed script, or a human approval.

## Where it fits

The orchestrator is the hub that wires every other runtime subsystem together. It consumes a [graph](graph.md) as input, dispatches [agent](agents.md) nodes through an `AgentRunner`, dispatches [execution unit](execution-units.md) nodes through an `ExecutableUnitRunner`, consults [conditions](conditions.md) via `NextStepPlanner` at each branching edge, checks `PolicyGuard`, writes `NodeAuditRecord` entries, and persists each step to `RunRepository`. Everything downstream of "I have a published graph" flows through it.

## Key types

- **`RuntimeOrchestrator`** — the dataclass engine; call `run_graph(graph, input, deployment_ref=...)` to execute.
- **`OrchestratorError`** — base exception raised when orchestration fails.
- **`NodeDispatcherError`** — raised when no runner is registered for a node type.
- **`Run`** — persistent record of an orchestrator execution (from `zeroth.core.runs`), updated after every step.
- **`AgentRunner` / `ExecutableUnitRunner`** — injected dispatch interfaces the orchestrator calls into for each node.

## See also

- [Usage Guide: orchestrator](../how-to/orchestrator.md)
- [Concept: graph](./graph.md)
- [Concept: agents](./agents.md)

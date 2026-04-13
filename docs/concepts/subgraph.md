# Subgraph Composition

*Added in v4.0*

A graph can reference another published graph as a nested subgraph node. The orchestrator enters the subgraph as a scoped execution that inherits governance, optionally shares thread memory, and propagates approvals back to the parent.

## How It Works

A `SubgraphNodeData` in a parent graph references a child graph by name (and optionally version). At runtime, the `SubgraphResolver` looks up the deployed child graph, namespaces its node IDs to prevent collisions with a `subgraph:{ref}:{depth}:` prefix, and merges the parent's governance as a ceiling the child can restrict but not relax. The `SubgraphExecutor` creates a child `Run` linked to the parent via `parent_run_id` and drives execution through the orchestrator's `_drive()` loop.

## Key Components

- **`SubgraphNodeData`** -- Pydantic model defining a subgraph node with `graph_ref`, `version`, `thread_participation`, and `max_depth` settings.
- **`SubgraphResolver`** -- Resolves graph references via `DeploymentService`, namespaces node IDs with `subgraph:{ref}:{depth}:` prefix, and merges parent governance. Import from `zeroth.core.subgraph.resolver` to avoid circular imports.
- **`SubgraphExecutor`** -- Creates child Run with `parent_run_id`, drives execution, and returns output to parent. Lazy-imported to avoid circular dependencies with graph models.

## Thread Participation

- **`inherit`** (default) -- Child shares the parent's `thread_id`, so agents in the subgraph participate in the same thread memory.
- **`isolated`** -- Child gets a new thread, fully isolated from the parent's memory.

## Approval Propagation

If a `HumanApprovalNode` inside a subgraph pauses execution, the parent run transitions to `WAITING_APPROVAL` with metadata linking to the pending subgraph. When the approval is resolved, the resume cascades from parent to child, the child completes, and its output flows back to the parent.

## Depth and Cycle Protection

- `max_depth` (configurable, default 3, max 10) prevents unbounded nesting.
- Visited-ref tracking detects cycles where a subgraph references itself or an ancestor, raising `SubgraphCycleError`.

## Error Handling

- **`SubgraphResolutionError`** -- Raised when the referenced graph cannot be found or resolved.
- **`SubgraphDepthLimitError`** -- Raised when nesting exceeds `max_depth`.
- **`SubgraphCycleError`** -- Raised when a cycle is detected in subgraph references.
- **`SubgraphExecutionError`** -- Raised when the child graph execution fails.

## Known Limitations

SubgraphNode cannot be used inside parallel fan-out branches (see [Parallel Execution](parallel.md#known-limitations)).

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.subgraph` for implementation.

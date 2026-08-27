# Conditions

## What it is

A **condition** decides, at run time, which route a workflow takes. Studio authors new decisions as an explicit **If node** with named `True` and `False` ports. The condition is configured on the node, so the canvas does not rely on floating edge tags to explain its logic.

The lower-level graph API also supports a `Condition` attached to an edge. That representation remains available for legacy and programmatic graphs during the compatibility window. The `zeroth.contracts.conditions` subsystem evaluates those edge rules, resolves which outgoing edge(s) win, and records the outcome on the run's audit trail.

## Why it exists

Real workflows branch: "if the classifier returns `refund`, route to the refund agent; otherwise escalate." Embedding that logic inside agents couples control flow to prompts and makes it invisible to reviewers. Putting the decision in an explicit Studio If node makes the authored canvas self-documenting: reviewers can see the decision, its expression, and its named outcomes as one control-flow unit. For legacy or code-authored graphs, a declarative edge `Condition` provides the equivalent inspectable rule. Both representations feed the audit log so every branch taken is reconstructable after the fact.

## Where it fits

Conditions sit between the [graph](graph.md) and the [orchestrator](orchestrator.md). The structured-token runtime dispatches a Studio-authored `IfNode` and emits one named route. In the legacy edge model, the orchestrator hands outgoing [`Edge`](graph.md) objects plus the current `TraversalState` to a `NextStepPlanner`, which evaluates each edge's `Condition` via `ConditionEvaluator` and returns a `NextStepPlan`. Conditions can consult [agent](agents.md) outputs, [execution unit](execution-units.md) results, and run variables through a `ConditionContext`.

## Key types

- **`NextStepPlanner`** — the top-level planner the orchestrator calls at each branching step.
- **`IfNode` / `IfNodeData`** — the first-class two-way control node authored in Studio.
- **`ConditionEvaluator`** — evaluates a single `Condition.expression` against a `ConditionContext`.
- **`BranchResolver`** — reduces multiple evaluated outcomes into a `BranchResolution` (which edges fire).
- **`ConditionBinding` / `ConditionBinder`** — compile-time bridge that attaches `Condition` objects to edges before a run starts.
- **`ConditionResultRecorder`** (runtime-owned, in `zeroth.runtime.runs`) — persists each decision to the audit trail so branches are inspectable later.

## See also

- [Usage Guide: conditions](../how-to/conditions.md)
- [Concept: graph](./graph.md)
- [Concept: orchestrator](./orchestrator.md)

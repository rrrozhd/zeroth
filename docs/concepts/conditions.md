# Conditions

## What it is

A **condition** is the rule attached to a graph edge that decides, at run time, whether the orchestrator should traverse it. The `zeroth.core.conditions` subsystem evaluates those rules, resolves which outgoing edge(s) win at a branching node, and records the outcome on the run's audit trail.

## Why it exists

Real workflows branch: "if the classifier returns `refund`, route to the refund agent; otherwise escalate." Embedding that logic inside agents couples control flow to prompts and makes it invisible to reviewers. Putting it on the edge, as a declarative `Condition`, makes the graph self-documenting: anyone can read `graph.edges` and see exactly how the run decides. The conditions subsystem provides the evaluator, branch resolver, and recorder that turn those declarations into actual routing decisions — all while feeding the audit log so every branch taken is reconstructable after the fact.

## Where it fits

Conditions sit between the [graph](graph.md) and the [orchestrator](orchestrator.md). When the orchestrator finishes a node, it hands the outgoing [`Edge`](graph.md) list plus the current `TraversalState` to a `NextStepPlanner`, which evaluates each edge's `Condition` via `ConditionEvaluator` and returns a `NextStepPlan` telling the orchestrator which node(s) to visit next. Conditions can consult [agent](agents.md) outputs, [execution unit](execution-units.md) results, and run variables through a `ConditionContext`.

## Key types

- **`NextStepPlanner`** — the top-level planner the orchestrator calls at each branching step.
- **`ConditionEvaluator`** — evaluates a single `Condition.expression` against a `ConditionContext`.
- **`BranchResolver`** — reduces multiple evaluated outcomes into a `BranchResolution` (which edges fire).
- **`ConditionBinding` / `ConditionBinder`** — compile-time bridge that attaches `Condition` objects to edges before a run starts.
- **`ConditionResultRecorder`** — persists each decision to the audit trail so branches are inspectable later.

## See also

- [Usage Guide: conditions](../how-to/conditions.md)
- [Concept: graph](./graph.md)
- [Concept: orchestrator](./orchestrator.md)

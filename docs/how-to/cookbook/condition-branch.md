# Branch execution on a condition

## What this recipe does
Builds a tiny classifier graph with two outgoing edges (approve vs
reject) guarded by expressions on the payload, and uses
`BranchResolver` to pick the active branch for each input.

## When to use
- A node produces a score or label that should steer execution to
  different downstream nodes.
- You want the branching logic in the graph itself — declarative,
  auditable, and inspectable — rather than inside an agent prompt.
- You need the same expression language across every edge, with a
  single evaluator you can reason about.

## When NOT to use
- The branching depends on a human decision — use an approval node.
- The expression cannot be captured as a safe AST subset (arbitrary
  Python, imports, function calls) — move the decision into an
  executable unit instead.

## Recipe
```python
--8<-- "condition_branch.py"
```

## How it works
`BranchResolver` walks every outgoing edge from the source node,
binds each `Edge.condition` into a `ConditionBinding`, and evaluates
the expression against a `ConditionContext` populated with the
payload. The evaluator (`_SafeEvaluator`) only allows literals,
comparisons, boolean logic, arithmetic, and dotted path lookups, so
expressions cannot execute arbitrary code. The resolver returns a
`BranchResolution` naming the active edges and next nodes.

## See also
- [Usage Guide: conditions](../conditions.md)
- [Concept: conditions](../../concepts/conditions.md)
- [Concept: graph](../../concepts/graph.md)

# Add a human approval step to a node

## What this recipe does
Inserts a `HumanApprovalNode` between an agent and a tool in your graph,
pauses the run when it reaches the gate, and resolves the approval via
`ApprovalService.resolve` so the downstream tool fires.

## When to use
- A node has real-world side effects (payment, email, production write)
  that must not happen without a human in the loop.
- A compliance policy requires an auditable sign-off event on specific
  node types before execution continues.
- You want a soft gate that can escalate via webhook if no one resolves
  it within the SLA.

## When NOT to use
- The decision is deterministic — a `Condition` on an edge is simpler
  and does not block on a human.
- The run must never pause — use a policy check instead, which fails
  fast and terminates the run.

## Recipe
```python
--8<-- "approval_step.py"
```

## How it works
`bootstrap_service` wires an `ApprovalService` against the same SQLite
store as the run and audit repositories. The orchestrator detects the
`HumanApprovalNode`, transitions the run to `WAITING_APPROVAL`, and
returns control. Calling `ApprovalService.resolve` with an
`ApprovalDecision.APPROVE` writes the decision, emits an audit record,
and lets the orchestrator resume at the downstream tool node.

## See also
- [Usage Guide: approvals](../approvals.md)
- [Concept: approvals](../../concepts/approvals.md)
- [Concept: graph](../../concepts/graph.md)

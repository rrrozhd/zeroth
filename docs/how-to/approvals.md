# Usage Guide: Approvals

## Overview

This guide shows how to attach a human-approval gate to a graph node and resolve it programmatically. The shape matches `examples/approval_demo.py`: a `HumanApprovalNode` on the graph pauses the [run](../concepts/runs.md) into `RunStatus.WAITING_APPROVAL`, a reviewer calls `POST /deployments/{ref}/approvals/{id}/resolve` (or the in-process `ApprovalService.resolve(...)`), and the [orchestrator](../concepts/orchestrator.md) resumes from the exact pause point.

## Minimal example

```python
# Slice from examples/approval_demo.py — approval gate, resolved in-process.
from zeroth.core.examples.quickstart import build_demo_graph
from zeroth.core.graph import GraphRepository

# 1. Author a graph that contains a HumanApprovalNode.
graph = await graph_repository.create(build_demo_graph(include_approval=True))
await graph_repository.publish(graph.graph_id, graph.version)
deployment = await deployment_service.deploy("demo-approval", graph.graph_id, graph.version)

# 2. Drive the orchestrator — it pauses on the approval node.
paused = await bootstrap.orchestrator.run_graph(
    bootstrap.graph,
    {"message": "Say hello from zeroth-core."},
    deployment_ref=deployment.deployment_ref,
)
assert paused.status.value == "waiting_approval"

# 3. Find the pending ApprovalRecord for the run.
pending = await bootstrap.approval_service.list_pending(
    run_id=paused.run_id,
    deployment_ref=deployment.deployment_ref,
)
approval_id = pending[0].approval_id

# 4. Resolve it over the real HTTP endpoint the curl docs show.
resolve = await client.post(
    f"/deployments/{deployment.deployment_ref}/approvals/{approval_id}/resolve",
    headers={"X-API-Key": "demo-operator-key"},
    json={"decision": "approve"},
)
resolve.raise_for_status()
print(resolve.json()["run"]["status"])  # -> completed
```

That is the full pattern: **enqueue** (the orchestrator creates the pending record when it hits the node) and **decide** (the reviewer POSTs a decision; the orchestrator resumes).

## Common patterns

- **Allow edits.** Set `allow_edits=True` on the node's `approval_policy_config` and the reviewer can POST `{"decision": "edit_and_approve", "edited_payload": {...}}` to patch the payload in flight.
- **SLA + escalation.** Set `sla_deadline` on the `HumanApprovalNode`; the `ApprovalSLAChecker` will mark overdue records `ESCALATED` and fire webhooks.
- **List pending for a dashboard.** `GET /deployments/{ref}/approvals?status=pending` drives operator UIs.
- **Reject cleanly.** `{"decision": "reject"}` terminates the run with `failure_state.reason == "approval_rejected"`; the rejection is audit-logged via `ApprovalActionRecord`.

## Pitfalls

1. **Resolving an already-resolved approval.** `ApprovalService.resolve` raises if the record is not `PENDING` — always check `list_pending` first.
2. **Wrong role.** The `X-API-Key` credential must carry `ServiceRole.OPERATOR` or `ServiceRole.REVIEWER`; admin-only keys cannot resolve approvals.
3. **Forgetting the deployment_ref.** `list_pending(run_id=..., deployment_ref=...)` needs both to scope within a tenant.
4. **Sensitive payloads in summaries.** The `summary` and `rationale` fields are shown verbatim to reviewers — keep secrets in `context_excerpt`, which runs through `PayloadSanitizer`.
5. **Assuming synchronous completion.** Over HTTP, `POST /runs` returns the *current* state; if the orchestrator is durable-worker-backed, the run may still be mid-flight when the response arrives.

## Reference cross-link

- Python API: [`zeroth.core.approvals`](../reference/python-api.md#approvals)
- Example source: `examples/approval_demo.py` (full runnable version).
- Related: [Concept: approvals](../concepts/approvals.md), [Usage Guide: policy](policy.md), [Usage Guide: audit](audit.md), [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md).

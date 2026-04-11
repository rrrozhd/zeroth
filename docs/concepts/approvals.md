# Approvals

## What it is

The **approvals** subsystem pauses a run at a `HumanApprovalNode`, persists a pending `ApprovalRecord`, and only lets the [orchestrator](orchestrator.md) continue once a reviewer resolves it via the Approvals API. It is the human-in-the-loop gate Zeroth ships out of the box — no custom wiring, no side-channel queues.

Approvals are a first-class graph node type, not an escape hatch. That means they benefit from the same persistence, audit, and tenant scoping as every other node in the system.

## Why it exists

Some decisions must not be made autonomously: refunding a customer, publishing a document, spending over budget, shipping code.

A good governance story needs a first-class point where execution *stops*, state is durably captured, a reviewer sees exactly the proposed payload, and the run is resumed (or rejected) from the exact point it paused. Approvals make this a node type, not an afterthought: authors drop a `HumanApprovalNode` on the graph, and the orchestrator + `ApprovalService` handle pause / persist / resume / audit without the author writing any integration glue.

Because approvals are driven by graph topology, the reviewer sees the exact input that would have flowed into the *next* node — no reconstruction, no guessing.

## Where it fits

Approvals sit *between* [policy](policy.md) and [audit](audit.md). Policy decides whether a node may run at all; once a node is allowed, if it is a `HumanApprovalNode`, the orchestrator transitions the [run](runs.md) to `RunStatus.WAITING_APPROVAL` and calls `ApprovalService.create_pending(...)`.

A human (authenticated by [identity](identity.md) and scoped by role) later POSTs `/deployments/{ref}/approvals/{id}/resolve`. `ApprovalService.resolve(...)` writes an `ApprovalActionRecord` into the [audit](audit.md) trail and tells the orchestrator to resume. Every step is recorded, so "who approved what, when" is never a question.

## Key types

- **`HumanApprovalNode`** — the graph node type that triggers a pause (declared in `zeroth.core.graph`).
- **`ApprovalRecord`** — the persistent pending-or-resolved request: `run_id`, `node_id`, `summary`, `rationale`, `proposed_payload`, `allowed_actions`, `sla_deadline`.
- **`ApprovalStatus`** — `PENDING`, `RESOLVED`, `ESCALATED`.
- **`ApprovalDecision`** — `APPROVE`, `REJECT`, `EDIT_AND_APPROVE` (the last lets a reviewer patch the payload before approving).
- **`HumanInteractionType`** — `APPROVAL`, `CLARIFICATION`, `REQUEST_INPUT`, `NOTIFICATION`.
- **`ApprovalResolution`** — the decision + `ActorIdentity` + `resolved_at` + any edited payload.
- **`ApprovalService`** — the orchestration API: `create_pending`, `list_pending`, `resolve`, `escalate`.
- **`ApprovalRepository`** — SQLite-backed storage for the records.

## See also

- [Usage Guide: approvals](../how-to/approvals.md) — attach an approval gate to a node the way `examples/approval_demo.py` does.
- [Concept: policy](policy.md) — what runs *before* the approval check.
- [Concept: audit](audit.md) — where every approval action is recorded.
- [Concept: identity](identity.md) — how reviewers are authenticated and role-scoped.
- [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md) — end-to-end policy + approval + audit story.

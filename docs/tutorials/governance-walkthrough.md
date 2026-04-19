# Governance Walkthrough

This tutorial exercises three Zeroth differentiators in a single
end-to-end run: an **approval gate** that pauses execution for human
review, an **auditor** that makes every node's decisions inspectable,
and a **policy** that blocks a tool call before it executes.

All three scenarios are driven by focused, single-purpose scripts
([`examples/20_approval_gate.py`](#scenario-1--approval-gate),
[`examples/21_policy_block.py`](#scenario-3--policy-block),
[`examples/24_audit_query.py`](#scenario-2--auditor)) plus an umbrella
runner [`examples/26_governance_walkthrough.py`](#full-example) that
sequences them. No mocks: each script talks to the real orchestrator
and, in the approval case, a real uvicorn instance.

## Why this matters

Most agent frameworks ship the agents and stop there. LangGraph,
CrewAI, and AutoGen let you wire up LLMs and tools, but they leave
*governance* — who approved this, what was logged, what the agent was
allowed to touch — as an exercise for the operator. Zeroth ships three
first-class subsystems for exactly that gap:

- **Approvals** — `HumanApprovalNode` pauses a run at a specific point
  in the graph until a reviewer resolves it via the Approvals API.
- **Audit** — every node emits a `NodeAuditRecord` containing inputs,
  outputs, enforcement decisions, and errors. The timeline is query-
  able by run, by deployment, or by node.
- **Policy** — `PolicyDefinition`s bind to nodes via
  `policy_bindings` and are enforced by `PolicyGuard` before the node
  runs. A denied capability terminates the run with
  `RunStatus.TERMINATED_BY_POLICY` and an audit record explaining why.

This walkthrough is the shortest path to seeing all three work
together on one graph.

## Prerequisites

- You have completed [Install](getting-started/01-install.md) and
  [First graph](getting-started/02-first-graph.md) from Getting
  Started.
- `OPENAI_API_KEY` is set in your environment (any litellm-supported
  provider works; OpenAI is the default the example uses).

!!! note "API key required"
    The example SKIPs cleanly (exit 0 with a stderr notice) when
    `OPENAI_API_KEY` is unset, so CI on forked PRs without secrets
    stays green. To actually see the walkthrough run, export the key
    before invoking it.

## Running the walkthrough

```bash
uv run python examples/26_governance_walkthrough.py
```

You should see three labelled sections — approval gate, policy block,
audit query — ending with `all governance scenarios passed.`

## Scenario 1 — Approval gate

The first scenario deploys a graph built with
`build_demo_graph(include_approval=True)`, which inserts a
`HumanApprovalNode` between the agent and the downstream tool node.
When the orchestrator reaches the approval node it pauses the run with
`RunStatus.WAITING_APPROVAL` and records a pending `ApprovalRecord`
against the run.

The example then lists pending approvals for the run, takes the
`approval_id`, and POSTs to the real
`POST /deployments/{ref}/approvals/{approval_id}/resolve` endpoint
with `{"decision": "approve"}`. The approval API hands the run back
to the orchestrator via `continue_run`, which drives it to
`COMPLETED`. The response body of the resolve call includes the
terminal run, so the script prints the final status inline.

This is the same code path a human operator would hit from a curl
command against a production uvicorn daemon — the tutorial just skips
the daemon by mounting the FastAPI app on `httpx.ASGITransport`.

## Scenario 2 — Auditor reviews the trail

After the approval scenario succeeds, the example fetches
`GET /runs/{run_id}/timeline`. The response is an
`AuditTimelineResponse` containing an ordered list of
`NodeAuditRecord`s — one per node attempt, with the agent node's
input/output snapshot, the approval node's decision, and the tool
node's output.

What "audit trail" means in Zeroth is *per-node, structured*, not a
monolithic log stream. Every `NodeAuditRecord` has a stable schema
(`node_id`, `status`, `input_snapshot`, `output_snapshot`,
`execution_metadata`, `error`) and is queryable by run, thread,
deployment, or node. The `execution_metadata` field holds any
`enforcement` context the `PolicyGuard` attached to that attempt.
The example prints the `node_id`, `status`, and any policy note for
each entry so you can see the whole decision log at a glance.

## Scenario 3 — Policy block

The third scenario is where Zeroth's policy layer earns its keep. The
example deploys a second graph using
`build_demo_graph_with_policy(denied_capabilities=[Capability.NETWORK_WRITE])`,
which binds `policy_bindings=["block-demo-caps"]` and the
`NETWORK_WRITE` capability to the tool node. It then:

1. Bootstraps a second in-process service against that deployment.
2. Wires a `PolicyGuard` onto the orchestrator with a
   `PolicyDefinition(policy_id="block-demo-caps",
   denied_capabilities=[Capability.NETWORK_WRITE])` registered in the
   `PolicyRegistry`, and each `Capability` value registered in the
   `CapabilityRegistry` under its own ref.
3. Drives a run against the blocked graph.

The orchestrator reaches the tool node, invokes `PolicyGuard.evaluate`,
sees the denied capability, and terminates the run. In terms of the
run lifecycle:

- Internal status: `RunStatus.FAILED` with
  `failure_state.reason == "policy_violation"`.
- Public HTTP status (via `/runs/{run_id}` or the response to
  `POST /runs`): `RunPublicStatus.TERMINATED_BY_POLICY`.

The orchestrator also writes a rejected `NodeAuditRecord` whose
`execution_metadata.enforcement` contains the denial decision and
reason. The example fetches
`GET /deployments/{ref}/audits?run_id=...`, filters for records whose
enforcement decision is `"deny"`, and prints them so you can see the
exact capability that tripped the policy.

This is the full denial loop — policy definition → binding → guard
evaluation → run termination → audit record — demonstrated against the
real runtime without mocks.

## Full example

```python
--8<-- "26_governance_walkthrough.py"
```

## Where to next

For deeper reading, see the subsystem concept pages under
[Concepts](../concepts/index.md). The source of truth for each
subsystem is:

- **Approvals** — `zeroth.core.approvals.service.ApprovalService`
  and `zeroth.core.service.approval_api`.
- **Audit** — `zeroth.core.audit.models.NodeAuditRecord` and
  `zeroth.core.service.audit_api`.
- **Policy** — `zeroth.core.policy.models.PolicyDefinition`,
  `zeroth.core.policy.guard.PolicyGuard`, and
  `zeroth.core.policy.registry`.

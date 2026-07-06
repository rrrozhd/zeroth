# Policy

## What it is

A **policy** is a named rule set that declares which capabilities a node is allowed — or forbidden — to use at runtime. `PolicyGuard` evaluates those rules **before** a node executes, returning an `EnforcementResult` that either lets the orchestrator proceed or terminates the run with reason `policy_violation`.

Policies are data, not code. They live in a `PolicyRegistry`, bind to graphs and nodes by `policy_id`, and are safe to diff, review, and version independently of the graph logic they govern.

Zeroth's policy layer is intentionally boring: it does not attempt to solve every governance problem in one place. It answers one question — *"should this node be allowed to run, given the capabilities it declares?"* — and hands the rest of the story (human sign-off, rate limiting, cost caps, audit) to dedicated subsystems.

## Why it exists

Agent frameworks happily hand an LLM a tool and hope for the best. Zeroth assumes the opposite: *every* node declares what it needs (network read, filesystem write, secret access, external API call, …), and *every* graph carries at least one `PolicyDefinition` that says which of those needs are legal in this deployment.

When a graph author wires a new tool node, the policy layer asks, "is this node allowed to touch the network here?" If the answer is no, the node never runs — a denial audit record is written and the run ends deterministically. Policy is the first, cheapest governance layer, evaluated statically from bindings without needing to execute anything expensive or call out to an LLM.

Because policies are just `PolicyDefinition` rows in a registry, a security reviewer can audit exactly which network-writing capability is legal in which deployment without reading a single line of agent code.

## Where it fits

Policy is the *pre-flight* check. [Orchestrator](orchestrator.md) collects `policy_bindings` from the [graph](graph.md) and the current node, asks `PolicyGuard.evaluate(...)` for a verdict, and either proceeds to the node's runner or fails the run.

If the node is permitted but still needs human sign-off, the orchestrator then hands off to [approvals](approvals.md). Either way, the `EnforcementResult` is stamped into the [audit](audit.md) trail via `NodeAuditRecord.execution_metadata["enforcement"]` so reviewers can replay exactly what each node was allowed to do.

Secrets and the sandbox runner consume `allowed_secrets` and `sandbox_strictness_mode` from the same result — policy is how capabilities flow through the rest of the runtime.

## Key types

- **`Capability`** — `StrEnum` of the nine capability kinds (`NETWORK_READ`, `NETWORK_WRITE`, `FILESYSTEM_READ`, `FILESYSTEM_WRITE`, `SECRET_ACCESS`, `EXTERNAL_API_CALL`, `PROCESS_SPAWN`, `MEMORY_READ`, `MEMORY_WRITE`).
- **`PolicyDefinition`** — a named rule set with `allowed_capabilities`, `denied_capabilities`, `allowed_secrets`, `network_mode`, and optional timeout / sandbox overrides.
- **`PolicyDecision`** — the binary verdict: `ALLOW` or `DENY`.
- **`EnforcementResult`** — the full evaluation output: decision, reason, effective capabilities, and the constraints downstream subsystems (secrets, sandbox) must honor.
- **`PolicyGuard`** — the evaluator; `evaluate(graph, node, run, input_payload)` is the hot path the orchestrator calls before each node.
- **`PolicyRegistry` / `CapabilityRegistry`** — in-memory stores for named policies and capability refs.
- **`apply_secret_policy`** — helper that filters an environment dict down to the `allowed_secrets` of the current enforcement result.

## See also

- [Usage Guide: policy](../how-to/policy.md) — block a tool call with a one-rule policy.
- [Concept: approvals](approvals.md) — the next governance layer after a policy allows a node.
- [Concept: audit](audit.md) — where enforcement decisions are persisted.
- [Concept: guardrails](guardrails.md) — the *runtime* protective layer that complements the policy pre-flight check.
- [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md) — end-to-end policy + approval + audit story.

# Usage Guide: Policy

## Overview

This guide shows how to declare a `PolicyDefinition` that denies a specific [capability](../concepts/policy.md) and wire it into the [orchestrator](../concepts/orchestrator.md) via `PolicyGuard` so that a tool node which requires that capability is **blocked before it executes**. The pattern mirrors the policy-block scenario in `examples/governance_walkthrough.py`.

## Minimal example

```python
from zeroth.core.policy import PolicyGuard
from zeroth.core.policy.models import Capability, PolicyDefinition
from zeroth.core.policy.registry import CapabilityRegistry, PolicyRegistry

# 1. Declare a policy that denies one capability.
no_network_write = PolicyDefinition(
    policy_id="block-network-write",
    denied_capabilities=[Capability.NETWORK_WRITE],
)

# 2. Register the policy and every Capability value under its own ref.
policy_registry = PolicyRegistry()
policy_registry.register(no_network_write)

capability_registry = CapabilityRegistry()
for cap in Capability:
    capability_registry.register(cap.value, cap)

# 3. Build the guard and hand it to the orchestrator.
guard = PolicyGuard(
    policy_registry=policy_registry,
    capability_registry=capability_registry,
)
orchestrator.policy_guard = guard  # RuntimeOrchestrator attribute
```

Bind the policy to the graph or node by adding its `policy_id` to `graph.policy_bindings` or `node.policy_bindings`. A tool node that declares `capability_bindings=["network_write"]` will now terminate with `RunStatus.FAILED` and `failure_state.reason == "policy_violation"` before running — the denial is written to the audit trail as `execution_metadata["enforcement"] = {"decision": "deny", "reason": "..."}`.

## Common patterns

- **Graph-wide allow list.** Attach one `PolicyDefinition` with `allowed_capabilities=[...]` to `graph.policy_bindings`; every node inherits it.
- **Node-local override.** Add a tighter policy to a single sensitive node via `node.policy_bindings` — stacked checks are AND-combined.
- **Secret scoping.** Set `allowed_secrets=[...]` on a policy and use `apply_secret_policy(...)` so nodes see only the variables they are permitted to read.
- **Sandbox strictness.** Set `sandbox_strictness_mode="strict"` or a `timeout_override_seconds` to propagate constraints into the sandboxed executable-unit runner.

## Pitfalls

1. **Forgetting to register capability values.** `CapabilityRegistry` is not pre-populated; if you don't register each `Capability` value, the guard cannot resolve node `capability_bindings` strings.
2. **Binding the policy but not attaching the guard.** `graph.policy_bindings` is inert until `orchestrator.policy_guard` is assigned a `PolicyGuard` instance.
3. **Expecting a denial to raise.** Policy denials terminate the run with `failure_state.reason == "policy_violation"`; they do not raise Python exceptions to the caller of `run_graph`.
4. **Mixing allow and deny lists carelessly.** `denied_capabilities` wins over `allowed_capabilities` when the same capability appears in both.
5. **Omitting audit review.** A blocked run still produces audit records — always query them after a denial to confirm *which* node tripped the rule.

## Reference cross-link

See the [Python API reference for `zeroth.core.policy`](../reference/python-api/policy.md).

Related: [Concept: policy](../concepts/policy.md), [Usage Guide: approvals](approvals.md), [Usage Guide: audit](audit.md), [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md).

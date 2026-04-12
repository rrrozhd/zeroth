# Block a tool call via policy

## What this recipe does
Registers a `PolicyDefinition` that denies `Capability.NETWORK_WRITE`
and evaluates `PolicyGuard` against a graph node whose
`capability_bindings` include that capability — showing the exact
`DENY` decision the orchestrator acts on at run time.

## When to use
- A tool can perform dangerous actions (network writes, filesystem
  escalation) and you want to block calls that don't satisfy the
  policy bindings on the node.
- You want the denial to be a hard failure — the run terminates with
  `failure_state.reason == "policy_violation"` instead of pausing.
- You need an auditable "why was this blocked" record attached to the
  run's audit trail.

## When NOT to use
- You want a soft gate a human can override — use an approval node.
- You want to cap spend — use a budget enforcer, not a policy.

## Recipe
```python
--8<-- "21_policy_block.py"
```

## How it works
`CapabilityRegistry` is not self-populating — you register each
`Capability` value explicitly so node `capability_bindings` can be
resolved by string. `PolicyRegistry.register` stores the policy under
the id the graph node binds to. At evaluation time, `PolicyGuard`
collects required capabilities from the node, compares them against
the `denied_capabilities` set, and returns a `DENY` `EnforcementResult`
with a human-readable reason.

## See also
- [Usage Guide: policy](../policy.md)
- [Concept: policy](../../concepts/policy.md)
- [Concept: execution-units](../../concepts/execution-units.md)

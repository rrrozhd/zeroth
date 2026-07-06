# Hand off between two agents mid-graph

## What this recipe does
Runs a researcher agent, pipes its `output_data` into a writer agent,
and then lets the orchestrator drive the second agent through the
quickstart graph end-to-end. Both agents are deterministic stubs so
no LLM credentials are required.

## When to use
- One agent specializes in gathering facts and another in producing
  prose — the two-step shape gives you clean audit boundaries.
- You want a role split (planner → executor, critic → writer) that
  surfaces as separate `NodeAuditRecord` rows.
- You're composing agents behind a single deployment ref and want to
  keep each one independently testable.

## When NOT to use
- A single well-prompted agent would do — extra nodes cost extra
  dispatch overhead and audit noise.
- The handoff is conditional — use a condition-bearing edge instead
  of a straight chain.

## Recipe
```python
--8<-- "02_multi_agent.py"
```

## How it works
Every `AgentRunner` exposes `async run(input_payload, ...) ->
object_with_output_data_and_audit_record`. The orchestrator stores
runners on `orchestrator.agent_runners` keyed by node id, and wires
them in at `bootstrap_service` time. Chaining two agents is a matter
of calling the first, handing its `output_data` to the second, and
letting the graph's edge mappings (or a direct dict pass) carry the
payload across.

## See also
- [Usage Guide: agents](../agents.md)
- [Concept: agents](../../concepts/agents.md)
- [Concept: orchestrator](../../concepts/orchestrator.md)

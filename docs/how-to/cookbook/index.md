# Cookbook

The Cookbook is a collection of short, task-oriented recipes that cut
across multiple Zeroth subsystems. Each recipe is around 200 words, has
a **When to use** / **When NOT to use** block so you can scan for fit
in under ten seconds, and embeds a real runnable file from `examples/`
so you can copy, paste, and run it locally.

If you're looking for an overview of a single subsystem, start in
[Concepts](../../concepts/index.md). If you're looking for a guided
walkthrough of a single subsystem's API, see the
[Usage Guides](../index.md). The Cookbook is where you come when you
already know the pieces and want to see them snapped together.

## Recipes

- [Add a human approval step to a node](approval-step.md) — pause a
  node until a human approves, then resume.
- [Attach memory to an agent](attach-memory.md) — wire a memory
  connector so an agent can carry state across turns.
- [Cap a run's cost budget](budget-cap.md) — estimate LLM cost with
  `CostEstimator` and block calls that would exceed a per-run cap.
- [Sandbox a tool call](sandbox-tool.md) — run a tool in an isolated,
  allowlisted environment with `SandboxManager`.
- [Retry a failing webhook with backoff](webhook-retry.md) — jittered
  exponential backoff, dead-letter thresholds, and HMAC verification.
- [Block a tool call via policy](policy-block.md) — deny a
  `Capability` before the tool fires with `PolicyGuard`.
- [Query the audit trail for a run](audit-query.md) — walk every
  `NodeAuditRecord` for a run with `AuditRepository`.
- [Hand off between two agents mid-graph](agent-handoff.md) — chain a
  researcher agent into a writer agent.
- [Branch execution on a condition](condition-branch.md) — evaluate
  edge conditions with `BranchResolver` + `ConditionContext`.
- [Inject a secret into an execution unit](secret-injection.md) —
  resolve secret refs into env vars and redact them from audit.

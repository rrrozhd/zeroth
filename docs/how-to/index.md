# How-to Guides

How-to Guides are **task-oriented**. Every page answers "how do I
do X with Zeroth?" with a minimal runnable example, the common
patterns you'll reach for, and a list of named pitfalls to avoid.
Pair every Usage Guide with its sibling
[Concept](../concepts/index.md) page when you need the "why".

Zeroth follows the [Diátaxis](https://diataxis.fr/) model: Concepts
for understanding, Usage Guides for task-oriented instructions,
[Tutorials](../tutorials/index.md) for guided learning, and
[Reference](../reference/index.md) for look-up.

## Subsystem usage guides

### Execution

- [Graph](graph.md)
- [Orchestrator](orchestrator.md)
- [Agents](agents.md)
- [Execution units](execution-units.md)
- [Conditions](conditions.md)

### Data and state

- [Mappings](mappings.md)
- [Memory](memory.md)
- [Storage](storage.md)
- [Contracts](contracts.md)
- [Runs](runs.md)

### Governance

- [Policy](policy.md)
- [Approvals](approvals.md)
- [Audit](audit.md)
- [Guardrails](guardrails.md)
- [Identity](identity.md)

### Platform

- [Secrets](secrets.md)
- [Dispatch](dispatch.md)
- [Economics](econ.md)
- [Service](service.md)
- [Webhooks](webhooks.md)

## Cookbook

The [Cookbook](cookbook/index.md) is a collection of ten short
cross-subsystem recipes — each is a runnable task ("add an approval
step", "cap a run's cost budget", "sandbox a tool call") that cuts
across multiple subsystems. Every recipe has a
**When to use** / **When NOT to use** block and embeds a real
example from the `examples/` directory.

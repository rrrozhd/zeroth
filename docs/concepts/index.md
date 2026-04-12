# Concepts

The Concepts quadrant is **understanding-oriented**. Each page explains
what a subsystem is, why it exists, how it fits with the others, and the
handful of named types you'll encounter when reading Zeroth source code.
Pair every Concept with its sibling [Usage Guide](../how-to/index.md)
once you're ready to pick up the API.

Zeroth follows the [Diátaxis](https://diataxis.fr/) model: Concepts for
understanding, [Usage Guides](../how-to/index.md) for task-oriented
instructions, [Tutorials](../tutorials/index.md) for guided learning,
and [Reference](../reference/index.md) for look-up.

## Execution

The nodes, edges, and runners that move a payload through a graph.

- [Graph](graph.md) — the top-level workflow model: nodes, edges,
  mappings, conditions, and the repository that persists them.
- [Orchestrator](orchestrator.md) — the runtime that drives a graph,
  dispatches nodes, and terminates runs.
- [Agents](agents.md) — `agent_runtime`: `AgentRunner`, provider
  adapters, prompt assembly, and tool attachment.
- [Execution units](execution-units.md) — sandboxed tool runners,
  manifests, admission control, and environment preparation.
- [Conditions](conditions.md) — edge-bound expressions,
  `BranchResolver`, and safe AST evaluation.

## Data and state

The models, contracts, and stores that carry run state between steps.

- [Mappings](mappings.md) — edge-level payload transformations
  between nodes.
- [Memory](memory.md) — `MemoryConnector` protocol, ephemeral and
  persistent backends (`memory-pg`, `memory-chroma`, `memory-es`).
- [Storage](storage.md) — async database abstraction, SQLite +
  Postgres backends, and Alembic migrations.
- [Contracts](contracts.md) — Pydantic-model-backed input/output
  schemas resolved through `ContractRegistry`.
- [Runs](runs.md) — `Run`, `RunStatus`, failure state, threads, and
  the repository that persists every transition.

## Governance

The gates, audits, and identities that make Zeroth auditable.

- [Policy](policy.md) — capability-based access control with
  `PolicyGuard` and `PolicyDefinition`.
- [Approvals](approvals.md) — human-in-the-loop gates via
  `HumanApprovalNode` and `ApprovalService`.
- [Audit](audit.md) — `NodeAuditRecord`, hash-chained integrity, and
  `AuditRepository` queries.
- [Guardrails](guardrails.md) — rate limits, quotas, and
  token-bucket enforcement.
- [Identity](identity.md) — `ActorIdentity`,
  `AuthenticatedPrincipal`, `ServiceRole`, and tenant scoping.

## Platform

The cross-cutting infrastructure: secrets, dispatch, cost, service,
webhooks.

- [Secrets](secrets.md) — `SecretProvider`, `SecretResolver`, and
  `SecretRedactor`.
- [Dispatch](dispatch.md) — `RunWorker`, `LeaseManager`, and the arq
  wakeup channel.
- [Economics](econ.md) — `CostEstimator`, `InstrumentedProviderAdapter`,
  and `BudgetEnforcer` against Regulus.
- [Service](service.md) — `bootstrap_service`, FastAPI app factory,
  and the uvicorn entrypoint.
- [Webhooks](webhooks.md) — `WebhookService`, delivery worker, and
  HMAC-SHA256 signing.


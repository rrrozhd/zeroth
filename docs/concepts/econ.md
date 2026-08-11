# Economics

## What it is

The `zeroth.econ.analytics` subsystem — called **economics** in the docs and
`econ` in the source tree — is how Zeroth tracks the monetary cost of every
LLM call, enforces per-tenant budgets, and forwards the resulting cost
events to an external observability companion called **Regulus**.

## Why it exists

Multi-agent systems burn LLM spend in ways that are hard to predict. A
single graph run may make dozens of provider calls across several models.
Without first-class accounting, tenants cannot be billed fairly, platform
operators cannot stop runaway loops, and product owners cannot answer
"what did yesterday actually cost?". Zeroth answers these questions by
*instrumenting* the provider adapter layer: every model call emits a cost
event before the caller ever sees the response.

## Where it fits

`econ` wraps the provider adapter layer used by
[agents](agents.md) and the [orchestrator](orchestrator.md), so every token
that flows through a [run](runs.md) is costed in flight. The cost events
are forwarded to **Regulus**, whose SDK —
[`econ-instrumentation-sdk`](https://pypi.org/project/econ-instrumentation-sdk/),
pinned as a direct dependency in `pyproject.toml` — handles transport and
dashboarding. Regulus is an external service; Zeroth is the client.

## Tenant boundary

Operational economics data is tenant-partitioned. Capabilities,
implementations, events, outcomes, estimates, budgets, dashboard material,
enforcement state, connectors, and erasure receipts are all
`TENANT_SCOPED`. Only four shared reference resources are `GLOBAL`:
`roles`, `user_roles`, `pricing_catalog`, and `tool_pricing_catalog`.

The distinction is enforced at the persistence gateway, not by remembering to
add a `WHERE tenant_id = ...` clause. Every mapped resource declares one scope,
and authenticated routes bind a `ScopedSession` from the token's trusted
tenant/workspace claims. Inserts are stamped or checked, reads are constrained,
and ownership changes are rejected. A tenant value in JSON, query parameters,
or metadata is only an assertion: it must match the authenticated claim and is
never the source of authority.

This is the boundary between the related governance decisions:

- **G02 (authorization)** decides whether a principal and its roles may perform
  an operation.
- **G04 (structural tenancy)** decides which rows that authorized operation can
  reach. Passing G02 cannot widen G04.

The reserved tenant ID `default` exists only for explicit compatibility and
migration of historical single-tenant subjects. New deployments should
provision a real tenant ID.

## Key types

- **`InstrumentedProviderAdapter`** — Wraps any `ProviderAdapter`
  (LiteLLM, OpenAI, Anthropic, …) and emits a cost event on every call.
  This is the primary integration point.
- **`RegulusClient`** — Thin wrapper around the Regulus SDK's
  `InstrumentationClient`. Handles auth, base URL, and fail-open semantics.
- **`CostEstimator`** — Converts `(model, prompt_tokens, completion_tokens)`
  into USD using LiteLLM's pricing table.
- **`BudgetEnforcer`** — Pre-execution check against Regulus'
  `/dashboard/kpis` endpoint. TTL-cached, fail-open on Regulus outage.

## See also

- Usage Guide: [how-to/econ](../how-to/econ.md)
- Related: [runs](runs.md), [agents](agents.md), [orchestrator](orchestrator.md)

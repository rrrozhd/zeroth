# Economics

## What it is

The `zeroth.econ.analytics` subsystem — called **economics** in the docs and
`econ` in the source tree — is how Zeroth tracks the monetary cost of every
LLM call, enforces per-tenant budgets, and forwards the resulting cost
events to the bundled **Regulus** control plane.

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
are forwarded through `zeroth.econ.instrumentation` to **Regulus**. Both the
instrumentation client and the `zeroth.econ.plane` backend ship in
`zeroth-core`; the backend can be mounted in-process or run separately.

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

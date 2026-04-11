# Using economics (cost tracking & budgets)

## Overview

Zeroth's economics layer answers two operational questions on every run:
*how much did this cost?* and *am I allowed to spend more?*. The first is
answered by wrapping any provider adapter in `InstrumentedProviderAdapter`;
the second by consulting a `BudgetEnforcer` before each LLM call. Both
talk to the external **Regulus** backend via the
`econ-instrumentation-sdk` package listed as a direct dependency in
`pyproject.toml`.

## Minimal example

```python
from zeroth.core.econ import (
    BudgetEnforcer,
    CostEstimator,
    RegulusClient,
)
from zeroth.core.econ import InstrumentedProviderAdapter  # lazy-imported

regulus = RegulusClient(base_url="http://regulus.internal:9000")
estimator = CostEstimator()

# Wrap any ProviderAdapter (e.g. a LiteLLM-backed adapter) so every call
# emits a cost event into Regulus automatically.
adapter = InstrumentedProviderAdapter(
    inner=my_llm_adapter,
    regulus=regulus,
    estimator=estimator,
    tenant_id="acme-corp",
)

# Before executing a run, gate it on the tenant's remaining budget.
budget = BudgetEnforcer(regulus_base_url="http://regulus.internal:9000")
if not await budget.is_within_budget(tenant_id="acme-corp"):
    raise RuntimeError("Monthly LLM budget exhausted")

response = await adapter.complete(prompt="hello")
```

## Common patterns

- **Budget caps per tenant** — Set caps in the Regulus dashboard; Zeroth
  enforces them pre-call via `BudgetEnforcer` with a 30-second TTL cache.
- **Per-run cost ceilings** — Combine the instrumented adapter with a
  run-level counter in the orchestrator to abort runaway agents mid-flight.
- **Unit types & pricing overrides** — `CostEstimator` defers to LiteLLM's
  pricing data; override the table for self-hosted or contract-priced
  models.
- **Fail-open on outage** — Both the enforcer and the client tolerate
  Regulus being unreachable, so observability incidents never stop the
  product from running.

## Pitfalls

1. **Missing Regulus service** — Without a reachable Regulus, no cost
   data is collected; the system runs, but invoices drift from reality.
2. **SDK version skew** — `econ-instrumentation-sdk` is a direct
   dependency; pin Regulus server and client to compatible minors.
3. **Double instrumentation** — Wrapping an already-instrumented adapter
   double-counts every token. Wrap exactly once at bootstrap.
4. **Pricing drift** — LiteLLM updates pricing tables; stale `litellm`
   means stale USD numbers. Refresh on each release.
5. **Unbounded cache** — Budget decisions are TTL-cached (default 30s);
   shorten it for tight budgets, but do not set it to 0 — you will
   hammer Regulus.

## Reference cross-link

API reference for `zeroth.core.econ` will live under the Reference
quadrant (Phase 32). Related guides:
[concepts/econ](../concepts/econ.md) · [runs](../concepts/runs.md).

# Using economics (cost tracking & budgets)

## Overview

Zeroth's economics layer answers two operational questions on every run:
*how much did this cost?* and *am I allowed to spend more?*. The first is
answered by wrapping any provider adapter in `InstrumentedProviderAdapter`;
the second by consulting a `BudgetEnforcer` before each LLM call. Both use the
bundled **Regulus** integration: `zeroth.econ.instrumentation` emits events to
the `zeroth.econ.plane` backend shipped in `zeroth-core`. The backend can be
mounted in-process or run separately.

## Minimal example

```python
from zeroth.econ.analytics import (
    BudgetEnforcer,
    CostEstimator,
    RegulusClient,
)
from zeroth.econ.analytics import InstrumentedProviderAdapter  # lazy-imported

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
2. **Deployment version skew** — The client and backend ship together; run
   standalone Regulus processes from the same `zeroth-core` version as callers.
3. **Double instrumentation** — Wrapping an already-instrumented adapter
   double-counts every token. Wrap exactly once at bootstrap.
4. **Pricing drift** — LiteLLM updates pricing tables; stale `litellm`
   means stale USD numbers. Refresh on each release.
5. **Unbounded cache** — Budget decisions are TTL-cached (default 30s);
   shorten it for tight budgets, but do not set it to 0 — you will
   hammer Regulus.

## Reference cross-link

See the [Python API reference for `zeroth.econ.analytics`](../reference/python-api/econ.md).

Related guides: [concepts/econ](../concepts/econ.md) · [runs](../concepts/runs.md).

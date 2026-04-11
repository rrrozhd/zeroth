# Cap a run's cost budget

## What this recipe does
Uses `CostEstimator` to price a proposed LLM call in USD, then blocks
dispatch whenever the projected spend would exceed a per-run budget
cap. The same pattern that `BudgetEnforcer` uses against Regulus, but
fully offline.

## When to use
- You want a hard ceiling on how much a single run can spend on LLM
  calls, regardless of what the provider bill says at the end of the
  month.
- You want to surface "would exceed budget" errors to callers at the
  point of dispatch, not after the run completes.
- You need offline cost estimation for tests and local development.

## When NOT to use
- You need tenant-wide spend enforcement — wire `BudgetEnforcer`
  against a live Regulus backend instead, it caches and fails open.
- You need observability dashboards — emit cost events through
  `InstrumentedProviderAdapter` and let Regulus aggregate.

## Recipe
```python
--8<-- "budget_cap.py"
```

## How it works
`CostEstimator.estimate` wraps `litellm.cost_per_token`, which ships
with a baked-in pricing table for major providers. It returns a
`Decimal("0")` for unknown models instead of raising, so the check
degrades gracefully. The caller adds the estimate to the running
spend and compares against the cap before dispatching.

## See also
- [Usage Guide: econ](../econ.md)
- [Concept: econ](../../concepts/econ.md)
- [Concept: runs](../../concepts/runs.md)

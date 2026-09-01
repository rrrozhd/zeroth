# Reconcile a provider bill to workflow outcomes

## Bottom line

Import an immutable normalized provider cost statement, then ask Zeroth which
workflow versions and outcomes received the billed dollars and which dollars
remain unsupported. The report never promotes estimated telemetry into provider
truth and never hides an unmatched or ambiguous bucket.

This is the first paid-service boundary. It is an API primitive, not a hosted
billing connector, subscription system, or finance dashboard.

## Inspect the value boundary locally

Install only the backend economic service dependencies and create a complete
example pack in one command:

```bash
pip install "zeroth-core[regulus]"
zeroth-econ demo
```

The command writes `zeroth-economic-demo/` containing the JSON and Markdown
economic diagnostic, provider reconciliation, and a short reading guide. It
uses the real economic service layer with a fixed in-memory SQLite dataset; it
does not start the UI, require a JWT, or contact a provider.

The output is explicitly **synthetic example — not customer evidence**. It
shows that Zeroth can preserve outcome semantics, attribute observed failed-run
and repeated-attempt cost, segment breakage by cohort, and close measured
dollars to a provider statement. It does not prove savings or establish that a
customer's telemetry can be reconciled. The command refuses to overwrite an
existing output path so an earlier pack is not silently replaced.

Continue below with real instrumentation and a real provider export before
using a report for an operational or purchasing decision.

## Why the import is bucket-based

Provider cost exports are commonly aggregated by time plus provider-owned
dimensions such as project, workspace, API key, model, or line item. OpenAI's
[Usage and Costs APIs](https://platform.openai.com/docs/api-reference/usage)
and Anthropic's
[cost and usage exports](https://support.anthropic.com/en/articles/9534590-cost-and-usage-reporting-in-console)
are examples. A provider request ID is useful evidence when available, but it is
not a portable invoice-line join key.

Zeroth therefore imports normalized billed-cost buckets and matches each bucket
to measured execution evidence by:

1. tenant and provider;
2. half-open time interval (`period_start` inclusive, `period_end` exclusive);
3. optional model; and
4. optional exact `provider_dimensions` stored in execution metadata.

Within a matched bucket, billed dollars are allocated in proportion to measured
execution cost. The statement total remains provider truth. Telemetry cost is a
separate comparison channel.

## Prerequisites

- Apply econ migration `20260830_13`.
- Emit execution evidence with `metadata.provider` and, when available,
  `metadata.model` plus provider scope identifiers such as `project_id` or
  `workspace_id`.
- Use `cost_measurement=measured` for allocation-eligible costs. Estimated and
  unmeasured events remain visible but cannot receive billed dollars.
- Define success for every workflow version that should close to an outcome.
- Use an Admin JWT for import. Admin, Analyst, and Approver roles may read the
  report; Viewer cannot read organization billing evidence.

## Normalize and import a statement

The import is USD-only in this version. Statement and bucket amounts must be positive,
bucket IDs must be unique, bucket periods must fit inside the statement period,
and their exact decimal sum must equal `billed_total_usd`.

### OpenAI Costs API: offline normalization

For the first reproducible provider path, request one complete monthly page from
OpenAI's Costs endpoint, grouped by project. The provider Admin key is used by
`curl`; Zeroth never reads or stores it.

```bash
curl --get https://api.openai.com/v1/organization/costs \
  -H "Authorization: Bearer $OPENAI_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data-urlencode "start_time=$START_UNIX" \
  --data-urlencode "end_time=$END_UNIX" \
  --data-urlencode "limit=31" \
  --data-urlencode "group_by[]=project_id" \
  > openai-costs.json

zeroth-econ normalize-openai-costs \
  --input openai-costs.json \
  --statement-id openai-2026-08 \
  --output provider-statement.json
```

The normalizer accepts only the documented OpenAI Costs page shape, USD, and a
complete response (`has_more=false` and `next_page=null`). It fails without
writing output if another page is required. OpenAI's Costs response exposes
project and line-item dimensions, but not model. Zeroth therefore consolidates
line items within each day/project bucket, preserves the exact billed total,
and uses `project_id` as the matching dimension. It does not invent model-level
invoice attribution.

The current normalizer intentionally supports one complete page rather than
fetching with provider credentials. For a monthly range, set `limit` to the
number of requested daily buckets. Longer or paginated ranges must be split or
normalized externally until a real buyer establishes that an automatic
connector is necessary.

For other providers, use the normalized contract below. Do not infer a CSV
schema from screenshots or documentation prose; add another adapter only from
a real, versioned export sample.

### Provider-neutral normalized contract

```bash
curl -sS -X POST http://127.0.0.1:8001/v1/reconciliation/provider-bills \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "statement_id": "openai-2026-08",
    "provider": "openai",
    "period_start": "2026-08-01T00:00:00Z",
    "period_end": "2026-09-01T00:00:00Z",
    "currency": "USD",
    "billed_total_usd": "143.72",
    "source_kind": "cost_api",
    "buckets": [
      {
        "bucket_id": "project-a-gpt-5",
        "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-09-01T00:00:00Z",
        "amount_usd": "143.72",
        "model": "gpt-5",
        "provider_dimensions": {"project_id": "proj_a"}
      }
    ]
  }'
```

The server canonicalizes timestamps to UTC, sorts buckets for hashing, stores a
`sha256:` statement digest, and treats `(tenant, provider, statement_id)` as an
immutable identity. An exact replay returns `200`; the first import returns
`201`; changed content under the same identity returns `409`.

`statement_id` is also the report URL identifier. Use 1–192 letters, digits,
`.`, `_`, `:`, or `-`, beginning with a letter or digit.

Do not put API keys, prompts, responses, customer identifiers, or invoice PDFs
in `provider_dimensions`. Use only the minimum provider-owned scope identifiers
needed to make buckets disjoint.

## Read the closure report

```bash
curl -sS \
  -H "Authorization: Bearer $FINANCE_TOKEN" \
  http://127.0.0.1:8001/v1/reconciliation/provider-bills/openai/openai-2026-08/report
```

For the complete headless flow, save the normalized request as
`provider-statement.json` and let the installed backend CLI import and render it:

```bash
export ZEROTH_ECON_TOKEN="$ADMIN_TOKEN"
zeroth-econ reconcile \
  --statement provider-statement.json \
  --output provider-reconciliation.md
```

Use `--format json` for downstream automation. The command talks only to the
service API; it does not install or launch the UI and never reads a provider
credential.

The response carries these independent totals:

| Field | Meaning |
|---|---|
| `billed_total_usd` | Immutable provider statement total |
| `allocated_billed_usd` | Billed dollars assigned to workflow/version/outcome groups |
| `unreconciled_billed_usd` | Buckets with no measured evidence or ambiguous overlap |
| `telemetry_measured_usd` | Measured execution cost used as allocation weight |
| `telemetry_variance_usd` | Provider total minus matched measured telemetry |
| `unbilled_telemetry_usd` | Measured provider telemetry outside every bucket scope |
| `outcome_unresolved_usd` | Allocated billed dollars whose run lacks resolvable outcome semantics |

`allocations` preserves the provider bucket ID, model, and provider dimensions,
then groups billed and telemetry dollars by workflow ID, workflow version, and
`success`, `failure`, or `unresolved` outcome status. This source identity is
what makes project/workspace chargeback possible without guessing ownership.

The closure state is fail-closed:

| State | Meaning |
|---|---|
| `reconciled` | Every bucket is allocated, every allocated outcome resolves, and measured telemetry equals the bill |
| `allocated_with_variance` | Every bucket and outcome is allocated, but provider and telemetry totals differ |
| `outcomes_unresolved` | Every bucket is allocated, but some billed dollars cannot be assigned to success or failure |
| `unreconciled` | At least one billed bucket has no measured match or overlaps another bucket scope |

Overlapping buckets are rejected from allocation as a group. Zeroth will not
double-count an execution merely to make the provider total appear closed.

## Current limits

- This release accepts normalized JSON; it does not fetch provider data or store
  provider credentials.
- Negative credits, taxes, and invoice adjustments are not yet modeled. Import
  non-negative usage-cost buckets, not an unnormalized invoice PDF.
- Allocation is bucket-level and proportional, not proof of a request-level
  invoice join.
- Request-time reconciliation refuses periods above 50,000 execution events.
  A hosted organization product needs durable pre-aggregation before raising
  that operational bound.
- The statement and digest are durable but not yet signed into an external
  evidence bundle.

These limits are part of the product claim. A report with variance is useful
because it says exactly what finance cannot yet substantiate; it is not a failed
dashboard render.

## Roll back

Stop serving provider-bill routes and back up the database, then run:

```bash
uv run alembic -c alembic-econ.ini downgrade 20260830_12
```

This drops `provider_cost_buckets` and `provider_bills`. It preserves execution
events, outcomes, and immutable outcome definitions. Roll forward with
`upgrade head` before re-enabling reconciliation.

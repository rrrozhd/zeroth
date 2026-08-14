# Configure ingress guardrails

Ingress guardrails bound how quickly a deployed service accepts work and how
much work its replicas execute together. Policies are durable, tenant-scoped,
and shared by SQLite or PostgreSQL service replicas.

## Understand precedence and defaults

Each effective field is selected independently in this order:

1. deployment override;
2. tenant override;
3. product default.

| Field | Product default | Accepted values |
|---|---:|---:|
| `rate_limit_capacity` | `10` | `1`–`1,000,000` |
| `rate_limit_refill_rate` | `1` token/second | greater than `0`, up to `100,000` |
| `rate_limit_burst` | `0` | `0`–`1,000,000` |
| `quota_daily_limit` | unlimited | `1`–`1,000,000,000,000`, or `null` for unlimited |
| `backpressure_queue_depth` | `100` | `1`–`1,000,000` |
| `max_concurrency` | `8` | `1`–`10,000` |

An omitted field inherits from the next scope. Explicit `null` is accepted
only for `quota_daily_limit`; nulling another control is rejected with `422`.
Every successful edit appends an immutable revision. It does not reset live
rate buckets or quota counters.

## Inspect and change policy

Readers with deployment-read permission can inspect effective settings and
history. Deployment administrators can append changes:

```console
curl -H "X-API-Key: $ZEROTH_API_KEY" \
  http://127.0.0.1:8000/v1/deployments/demo/guardrails

curl -X PUT -H "X-API-Key: $ZEROTH_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rate_limit_capacity": 50, "rate_limit_burst": 10, "max_concurrency": 4}' \
  http://127.0.0.1:8000/v1/deployments/demo/guardrails
```

Use `/v1/guardrails` for tenant defaults and
`/v1/guardrails/history` for the tenant's immutable revision history. A
cross-tenant deployment reference is returned as not found.

The Deployments console provides the same six controls. A blank value means
inherit; `unlimited` explicitly disables the daily quota. Validation and save
failures remain inline so an operator can correct and retry without losing the
form values.

## Handle admission responses

The service coordinates queue, rate, and quota decisions with durable run
creation. Replicas also coordinate their shared running limit at lease claim.

| Decision | Result | Remediation |
|---|---|---|
| Rate bucket exhausted | `429 Too Many Requests` | Wait for `Retry-After`, then retry. |
| Queue full | `503 Service Unavailable` | Drain pending work or raise queue depth. |
| Daily quota exhausted | `503 Service Unavailable` | Wait for `Retry-After` or change the quota. |
| Shared concurrency saturated | Work remains queued | Wait for a running lease to finish or raise concurrency. |

Every HTTP rejection includes an integer `Retry-After` header derived from the
governing state. Do not retry before that interval.

## Observe and remediate saturation

`GET /v1/metrics` exposes:

- `zeroth_guardrail_admissions_total`;
- `zeroth_guardrail_rejections_total{reason="rate|queue|quota|concurrency"}`;
- `zeroth_guardrail_utilization_ratio{resource="rate|queue|quota|concurrency"}`;
- `zeroth_guardrail_queue_depth`;
- `zeroth_guardrail_policy_changes_total{scope="tenant|deployment"}`.

Rejected ingress decisions append scoped audit records under
`service.guardrail.<reason>`. Check the effective policy, queue depth, and
utilization together before raising a limit: a sustained full queue usually
means worker capacity or downstream latency is the bottleneck, while repeated
rate rejection with an empty queue means the token policy is the constraint.

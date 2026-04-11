# Using webhooks

## Overview

Webhooks let external systems react to run lifecycle events without
polling. You register a subscription (URL, secret, event types),
Zeroth emits payloads as runs progress, and a background delivery
worker POSTs them with HMAC-SHA256 signatures, exponential backoff,
and dead-letter capture. This page shows how to subscribe, how to
verify signatures on the receiver, and how to replay dead-letters.

## Minimal example

Register a subscription via the REST API (routes are mounted by
`create_app` from `zeroth.core.service.webhook_api`):

```bash
curl -X POST https://zeroth.example.com/webhooks/subscriptions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://listener.example.com/zeroth",
    "secret": "shhh",
    "event_types": ["run.completed", "run.failed"],
    "deployment_ref": "default"
  }'
```

Emit events from your own code when you need to (the orchestrator does
this automatically for run lifecycle events):

```python
from zeroth.core.webhooks import WebhookEventType, WebhookService

service = WebhookService(repository=webhook_repo)
await service.emit_event(
    event_type=WebhookEventType.RUN_COMPLETED,
    deployment_ref="default",
    tenant_id="acme-corp",
    data={"run_id": "run-abc", "duration_ms": 4200},
)
```

Verify a delivery on the receiving side:

```python
import hmac, hashlib
sig = request.headers["X-Zeroth-Signature"]
expected = hmac.new(b"shhh", request.body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(sig, expected)
```

## Common patterns

- **Signing secrets per subscription** — Never reuse one secret across
  tenants; rotate on compromise and keep both the old and new secret
  valid for a grace window.
- **Retry with backoff** — The delivery worker retries up to
  `max_attempts` (default 5) with exponential backoff before moving
  the record to the dead-letter table.
- **Dead-letter replay** — After a subscriber outage, operators can
  replay the dead-letter table through `WebhookService` so no event
  is lost.
- **Cross-link with dispatch** — Delivery retries flow through the
  same background worker lifecycle as run dispatch (see
  [dispatch how-to](dispatch.md)); graceful shutdown drains both.

## Pitfalls

1. **No signature verification on the receiver** — If you do not
   check `X-Zeroth-Signature`, an attacker can spoof events. Always
   verify.
2. **Idempotency** — Delivery is *at-least-once*. Use the stable
   `event_id` on `WebhookEventPayload` to deduplicate on your side.
3. **Timeouts tuned too high** — A slow subscriber blocks the worker
   slot; keep per-request timeouts under a few seconds and rely on
   retries for transient failures.
4. **Leaking secrets in logs** — Wire the webhook subscription
   secret through `SecretRedactor` (see [secrets how-to](secrets.md))
   before any log write.
5. **Forgetting dead-letter monitoring** — Dead-lettered events are
   invisible until an operator queries them. Alert on dead-letter
   growth, not just delivery failures.

## Reference cross-link

See the [Python API reference for `zeroth.core.webhooks`](../reference/python-api/webhooks.md).

Related guides: [concepts/webhooks](../concepts/webhooks.md) · [dispatch how-to](dispatch.md) · [secrets how-to](secrets.md).

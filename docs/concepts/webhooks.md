# Webhooks

> **Note on this page.** The Phase 31 content spec originally listed
> `threads` as the 20th subsystem. No `zeroth.core.threads` module
> exists in the current tree, so — with the planner's explicit
> discretion — this slot is filled by `zeroth.core.webhooks`, a
> concrete user-facing async integration surface that would otherwise
> have no documentation coverage. The substitution is also recorded in
> the plan frontmatter for auditability.

## What it is

The `zeroth.core.webhooks` subsystem is Zeroth's **outbound webhook
delivery system**: tenants subscribe to event types (run lifecycle,
approvals, cost thresholds), Zeroth emits events as runs progress, and
a delivery worker POSTs HMAC-signed payloads to subscriber URLs with
retry-with-backoff and dead-letter handling.

## Why it exists

Agent runs are long and asynchronous. Callers cannot sit on an HTTP
connection waiting for an answer that may arrive minutes later, behind
a human approval. Webhooks are the natural integration surface:
tenants register a URL, Zeroth promises *at-least-once* delivery with
retries, a signature, and an audit trail. The module exists to make
that promise durable — nothing is sent fire-and-forget, every attempt
is persisted, every failure is recoverable.

## Where it fits

Webhooks are emitted from [runs](runs.md) (and the orchestrator) via
`WebhookService.emit_event`, which fans a payload out to every matching
subscription. Delivery happens in a background worker started by the
[service](service.md) lifespan (alongside [dispatch](dispatch.md)), and
every attempt is recorded so the [audit](audit.md) trail can be queried
for exactly what a subscriber was told, when.

## Key types

- **`WebhookSubscription`** — A tenant's registration: `url`,
  `secret`, `event_types`, `deployment_ref`, active flag.
- **`WebhookEventPayload`** / **`WebhookEventType`** — The enum of
  emittable events and the Pydantic payload envelope.
- **`WebhookDelivery`** / **`DeliveryStatus`** — A single delivery
  attempt record with status, retry count, and last response.
- **`WebhookDeadLetter`** / **`EscalationAction`** — The terminal
  state after `max_attempts` is exceeded, plus any escalation hook.
- **`WebhookService`** — Emits events, manages subscriptions, replays
  dead-letters.
- **`WebhookRepository`** — Storage interface for subscriptions,
  deliveries, and dead-letters.
- **`sign_payload()`** — HMAC-SHA256 signer used on every outbound
  request.

## See also

- Usage Guide: [how-to/webhooks](../how-to/webhooks.md)
- Related: [runs](runs.md), [audit](audit.md), [dispatch](dispatch.md)

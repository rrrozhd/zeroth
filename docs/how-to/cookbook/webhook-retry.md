# Retry a failing webhook with backoff

## What this recipe does
Demonstrates the jittered exponential backoff schedule used by
`WebhookDeliveryWorker`, the dead-letter threshold after a fixed number
of attempts, and the HMAC-SHA256 verification a receiver performs on
every delivery.

## When to use
- You're building a webhook receiver and need to verify the
  `X-Zeroth-Signature` header before trusting the payload.
- You're tuning retry behaviour for a deployment that emits
  `run.completed` / `approval.resolved` events and want to know what
  the worker will do on transient failures.
- You need to reason about when a delivery will be marked
  `DeliveryStatus.DEAD_LETTER` so you can alert on it.

## When NOT to use
- You're delivering events synchronously and cannot tolerate retries
  — call the business logic directly instead of the webhook service.
- You need at-most-once semantics — retries are at-least-once by
  design.

## Recipe
```python
--8<-- "webhook_retry.py"
```

## How it works
`next_retry_delay(attempt)` returns a uniform random sample in
`[0, min(base * 2**attempt, max_delay)]`, so every retry is bounded
and jittered to avoid thundering-herd behaviour. The worker stops
retrying once the attempt count crosses `max_attempts` and writes a
`WebhookDeadLetter` row. Receivers use `sign_payload` (or a
`hmac.compare_digest` equivalent) to verify every delivery.

## See also
- [Usage Guide: webhooks](../webhooks.md)
- [Concept: webhooks](../../concepts/webhooks.md)
- [Concept: dispatch](../../concepts/dispatch.md)

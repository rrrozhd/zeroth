"""Retry a failing webhook with backoff — example for docs/how-to/cookbook/webhook-retry.md.

Demonstrates :func:`zeroth.core.webhooks.delivery.next_retry_delay` — the
jittered exponential backoff used by the production
:class:`WebhookDeliveryWorker` — and shows how a receiver verifies the
HMAC-SHA256 signature produced by :func:`zeroth.core.webhooks.sign_payload`.

Runs fully in-process; no HTTP server or Redis required.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0

    from zeroth.core.webhooks import sign_payload
    from zeroth.core.webhooks.delivery import next_retry_delay

    # 1. Backoff schedule: attempts 0..4 — upper bound doubles each time,
    #    capped at max_delay. The real worker samples uniformly in [0, bound].
    print("attempt  max_delay_bound")
    for attempt in range(5):
        # Use a large base so the printed schedule is easy to read.
        bound_upper = min(1.0 * (2**attempt), 300.0)
        sample = next_retry_delay(attempt, base=1.0, max_delay=300.0)
        print(f"  {attempt:>2}      <= {bound_upper:>6.1f}s  (sampled {sample:.2f}s)")

    # 2. Dead-letter threshold: production default is max_attempts=5; once
    #    an attempt reaches that number, the delivery transitions to
    #    DeliveryStatus.DEAD_LETTER and stops retrying.
    max_attempts = 5
    for attempt in range(1, max_attempts + 2):
        if attempt > max_attempts:
            print(f"attempt {attempt}: dead-letter")
        else:
            print(f"attempt {attempt}: retry scheduled")

    # 3. Receiver-side HMAC-SHA256 verification using the same helper that
    #    WebhookDeliveryWorker uses to sign outgoing payloads.
    secret = "shared-demo-secret"  # noqa: S105 — demo constant
    payload = b'{"event_type":"run.completed","deployment_ref":"demo","data":{}}'
    signature = sign_payload(payload, secret)
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected), "signature mismatch"
    print(f"verified signature: sha256={signature[:16]}…")

    print("webhook-retry demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

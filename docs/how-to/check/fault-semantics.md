# Mandatory fault semantics

Every unique curated side-effecting ActionIdentityV1 runs four faults:

- `duplicate_delivery`: A is held in flight while B receives the same envelope; one marker and one
  replayable receipt are required.
- `timeout_after_effect`: the marker is written before `TimeoutError`; state must be AMBIGUOUS and
  automatic retry must not add a marker.
- `cancellation_after_effect`: cancellation occurs after the marker and must propagate; ambiguity
  is durable and resume must not add a marker.
- `restart_after_receipt`: process A stores marker plus COMPLETED receipt and hard-exits before a
  graph checkpoint; fresh process B replays the receipt without another marker.

A fault counts only when both injection and recovery events occur in the required order. Missing
events are invalid, not a pass. The marker store is observational and never deduplicates; the
candidate-owned action repository provides the fence.

Zeroth does **not** claim at-most-once across the external-effect/receipt gap. If an effect may have
happened and no receipt is durable, the correct state is AMBIGUOUS until downstream idempotency or
operator-attributed reconciliation establishes an outcome.

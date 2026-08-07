# Delivery guarantees at the side-effect boundary

Zeroth's runtime is **at-least-once** at the point where it calls out to the
world. This page states exactly what that means, what the runtime does about it,
and — the part that matters most — what it still cannot promise on your behalf.

## Why at-least-once

A worker can apply an external effect and then die before recording that it did.
Nothing the runtime does locally removes that window: the write to the external
system and the write to Zeroth's own store are two separate acts, and no
transaction spans both. So a retry can, in principle, re-apply the effect.

What the runtime can do is make the repeat **recognisable**.

## Operation identity

Every side-effecting call carries an `OperationIdentity`. Its `operation_key` is
derived from the run, the idempotency key, the target, and a per-call ordinal.

Two exclusions are deliberate:

- **`attempt` is excluded**, so a transport retry, a token retry, and a recovered
  worker all reproduce the same key. That reproduction is what makes a repeat
  recognisable at all.
- **`dispatch_id` is excluded**, because crash recovery re-issues a dispatch for
  an unchanged logical operation. Including it would fork the identity at exactly
  the moment the guarantee is needed most.

The ordinal is what keeps two tool calls in one agent turn apart. Without it, the
second call would look like a duplicate of the first and be suppressed.

Integrations receive the identity as an explicit parameter on the executable-unit
boundary. They do not reconstruct it from run metadata.

## The five operation states

| State | Meaning |
|---|---|
| `NOT_STARTED` | No record exists. This is the *absence* of a row, not a stored value — writing a row before attempting the effect would itself be a durable act recovery could not distinguish from a real attempt. |
| `IN_FLIGHT` | An attempt is running, or one vanished without reporting. |
| `COMPLETED` | The effect landed and its receipt is stored. A later attempt returns the receipt instead of re-executing. |
| `FAILED` | The integration *reported* a failure. Safe to retry. |
| `AMBIGUOUS` | The outcome is unknown. Not a failure. |

The distinction between `FAILED` and `AMBIGUOUS` carries the weight. A refusal
the integration actually reported is a fact; a timeout is not. Folding the second
into the first would be asserting something the runtime does not know.

## Reconciliation is bounded

An ambiguous operation leaves durable reconciliation work behind. Reconciliation
has a finite attempt budget, and **exhausting it does not manufacture a verdict** —
the operation stays `AMBIGUOUS`. The budget stops the loop; it does not resolve
the uncertainty.

Duplicate outcome reports converge: the first `COMPLETED` result wins. Otherwise
the stored outcome would depend on arrival order between two workers that both
believe they own the operation.

## What remains at-least-once — read this part

The runtime cannot make a remote system idempotent. Each operation records which
guarantee actually applies, via `SideEffectSupport`:

| Support | What Zeroth can promise |
|---|---|
| `IDEMPOTENT` | The target accepts the operation key and collapses repeats itself. A duplicate call is absorbed downstream. |
| `OUTCOME_QUERYABLE` | The target cannot dedupe, but can be asked what a prior call did — so an ambiguous outcome can be resolved rather than guessed. |
| `AT_LEAST_ONCE` | **Neither.** A retry may apply the effect twice. This is the default. |

`AT_LEAST_ONCE` is the default on purpose: an integration that never declared
support gets the *weaker* guarantee, so the residual risk is visible rather than
implied away. When such an operation goes ambiguous, the record sets
`residual_duplicate_risk`, and the audit record carries it through.

For those integrations the honest summary is: **Zeroth will tell you that a
duplicate was possible. It cannot tell you that one did not happen.** Closing
that gap requires the integration to support an idempotency key or an outcome
query; there is no runtime-side substitute.

## Fencing stale workers

Leases carry a generation that advances on every claim and reclaim. Renewal is
qualified by owner *and* generation, and `commit_fenced` puts the fence inside
the `UPDATE` predicate rather than in a preceding check — a check-then-write
leaves precisely the window the fence exists to close.

When renewal reports ownership loss, the displaced worker's execution is
cancelled rather than merely logged, and it writes nothing further. In
particular it does not mark the run failed: the run is not failed, it simply
belongs to another worker now.

## Observability

Each outcome is counted separately rather than as one labelled counter, so
"how often did we suppress a replay" is answerable without a metrics backend
that supports label queries:

- `zeroth_side_effect_first_execution_total`
- `zeroth_side_effect_replay_suppressed_total`
- `zeroth_side_effect_ambiguous_total`
- `zeroth_side_effect_reconciliation_succeeded_total`
- `zeroth_side_effect_reconciliation_failed_total`
- `zeroth_lease_fencing_rejected_total`
- `zeroth_lease_lost_total`

Node audit records carry a `side_effect_operation` block with the operation key,
target, declared support, state, whether this was a first execution or a
suppressed replay, and the residual duplicate risk.

## Opting in

The receipt store is optional. Without one wired, the dispatch path is a
pass-through and behaves exactly as it did before — read-only and
side-effect-free nodes are unaffected either way.

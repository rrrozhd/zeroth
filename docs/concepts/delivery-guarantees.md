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

## What sits outside the boundary entirely

Everything above describes executable units, which reach the runtime through
`RuntimeToolExecutor`. **MCP tools do not — and they are graph nodes.**

An `mcp_tool` node is a first-class node, and an agent's call to one arrives at
the same `RuntimeToolExecutor` an executable unit does. It is governed on the
way: the call is routed through the run's MCP session pool, which applies the
capability gate before a server process exists and refuses to call a tool whose
live shape has drifted from what the graph pinned at import. What the executor
does *not* do is mint an operation identity — it returns before that point, on
purpose, because a receipt would imply a durability that does not exist here.
So an MCP call has:

- no operation identity and no durable operation record;
- no replay suppression — a retried agent turn calls the tool again;
- no reconciliation path for an ambiguous outcome.

Pinning constrains the tool's *shape*, not its delivery semantics. That is why
`mcp_tool` is its own node kind rather than a mode on `ExecutableUnitNode`,
where the weaker guarantee would sit invisibly beside nodes that really do
carry a receipt.

This is a real limit, not an oversight to be read past. It is marked rather than
implied: a tool-call audit record for an MCP call carries
`operation_support: at_least_once` and `operation_residual_duplicate_risk: true`
— on a call that *failed* after reaching the server as much as on one that
succeeded, because a failed MCP call is precisely the case that may have taken
effect anyway.

Read the absence of those fields carefully, though: it means two different
things. For an executable unit it means the operation guarantee covered the
call. For an MCP call it means the runtime refused *before* dispatch — unknown
server, capability denial, ceiling, a spawn that never completed its handshake,
schema drift — so the tool was never invoked and nothing happened.

Closing the gap for a side-effecting MCP tool takes the same thing it takes for
any `AT_LEAST_ONCE` integration above: idempotency or an outcome query on the
server's side. There is no runtime-side substitute, and no graph-side one —
what the registry and the pin *do* bound is covered in the
[MCP tools how-to](../how-to/mcp.md).

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

Node audit records carry the same facts as flat, individually-typed keys —
`operation_key`, `operation_target_ref`, `operation_support`, `operation_state`,
`operation_first_execution`, `operation_replay_suppressed`,
`operation_reconciliation_required`, `operation_reconciliation_exhausted`, and
`operation_residual_duplicate_risk`. Flat is load-bearing, not stylistic: the
audit capture boundary keeps an allowlisted, per-key-typed projection of
top-level keys, so a nested block is discarded before it is ever persisted.

## Opting in

The receipt store is optional. Without one wired, the dispatch path is a
pass-through and behaves exactly as it did before — read-only and
side-effect-free nodes are unaffected either way.

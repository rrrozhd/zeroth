# Default-on structured multi-token engine

Date: 2026-07-18

## Objective

Make the token/provenance join engine the default execution path and support
multi-token fan-out inside loops as core functionality. Keep the legacy engine
available through an explicit `sequential_join_enabled=False` compatibility
escape hatch for one migration window. Do not change the default until the
correctness and parity gates in this design pass.

## Decision

Use structured multi-token loop semantics.

Fan-out creates uniquely identified child tokens under the current loop-instance
and iteration tag. Children may reconverge inside the loop or exit independently.
The loop remains live until every child settles. Exit deliveries accumulate on
the loop instance, and the loop resolves its exit-edge unit exactly once after all
live children settle. This prevents concurrent exits from dispatching an
out-of-loop join more than once while retaining every delivered payload for the
configured reducer.

Two alternatives were rejected:

- Requiring every fan-out to reconverge before exit would keep useful batch and
  early-exit workflows unsupported.
- Allowing unstructured asynchronous exits would make termination, replay,
  cancellation, and exactly-once dispatch depend on timing rather than durable
  state.

## Runtime model

### Token identity and envelope

Every live control-flow unit has a stable `TokenId`. A persisted `TokenEnvelope`
contains:

- token ID and optional parent token ID;
- provenance tag for nested loop iterations;
- current node and causal inbound edge;
- input payload;
- retry attempt and lifecycle state when applicable.
- the durable fork lineage (`ForkId`, child creation ordinal, and optional
  `JoinInstanceId`) that identifies which siblings may reconverge.

Runtime queues contain envelopes. Payloads and tags are no longer staged in
node-ID-keyed side maps for multi-token execution.

### Fork and join cohorts

Every structural or parallel fan-out creates a durable `ForkInstance`. The
fan-out transition assigns a `ForkId`, registers the exact ordered child set,
gives each child a stable creation ordinal, and initializes an outstanding-child
count plus one settlement obligation per child. Nested fan-outs push another fork
frame onto the token's lineage; they never reuse or flatten an ancestor's cohort.

For every convergent target reachable within the structured fork, the runtime
creates a `JoinInstanceId` derived from the target, provenance tag, and innermost
open `ForkId`. Its durable obligation set names the exact child/edge resolutions
expected from that cohort. An active path delivers its registered resolution; a
suppressed, failed, or cancelled path settles that same registered resolution
with its explicit outcome. A resolution from another fork lineage cannot enter
the bucket.

A join is ready only when every registered obligation is settled and at least
one is delivered. It consumes the cohort's settled children, invokes the target's
`JoinConfig` in canonical child-ordinal order, creates one continuation token
whose parentage records the consumed token IDs, and closes the join instance.
Closing an inner fork resumes its parent token lineage; it cannot close or merge
an outer fork's siblings.

A fork does not require reconvergence to close. Every child eventually settles
its fork obligation as joined, exited, suppressed, failed, or cancelled. If the
children reconverge, the join transition closes the consumed obligations and
creates the continuation described above. If they settle independently (for
example through different loop exits), their ordered outcomes are atomically
transferred to the owning loop/iteration or parent structured scope. When the
outstanding-child count reaches zero, the runtime closes the `ForkInstance`,
resumes the parent lineage only when the owning scope requires a continuation,
and garbage-collects the fork after the transfer/continuation revision is durable.
An independently settled fork never manufactures a synthetic join token.

### Loop instance

A `LoopInstance` is keyed by its loop header and outer provenance tag. It stores:

- one durable `IterationFrame` per active iteration index;
- all live child token IDs;
- accumulated delivered exit payloads keyed by exit edge and token;
- resolved or suppressed exits;
- lifecycle state for running, stopping, or completed;
- enough causal state to restore the instance without recomputation.

The loop instance, not a source-node visit, owns loop termination.

### Iteration barrier and back edges

Each loop begins with exactly one active `IterationFrame`. All tokens created
inside that iteration belong to the frame until they settle as one of: internal
completion, back-edge continuation, exit delivery, suppression, failure, or
cancellation. Internal fan-out may create more frame members, but an iteration
cannot advance while any member is unsettled.

Back-edge arrivals are provisional continuation deliveries. When every frame
member settles:

- if one or more back-edge continuations were delivered, the frame freezes its
  exit deliveries on the parent `LoopInstance`, reduces continuation payloads in
  canonical fork-lineage/child-ordinal order using the destination header's
  `JoinConfig` (a configuration is required when more than one continuation can
  deliver), advances the iteration exactly once, and creates the next frame;
- if no back-edge continuation delivered, the loop terminates and finalizes its
  accumulated exits;
- a failure or cancellation applies the declared branch failure policy before
  either transition. Fail-fast cancels unsettled siblings; best-effort settles
  the failed child as suppressed only where the graph explicitly permits it.

Exit deliveries from an iteration that also continues remain accumulated and
are not emitted early. Iteration frames never overlap: the next frame becomes
dispatchable only after the prior frame transition is durable. For nested loops,
an inner loop's finalized continuation settles exactly one token outcome in its
owning outer iteration frame.

### Fan-out

Fan-out atomically retires its parent token and creates child token envelopes.
Either the full transition is persisted or none of it is. No child may become
dispatchable before the parent-to-children transition is durable.

### Reconvergence

A join bucket is keyed by target node, provenance tag, and `JoinInstanceId`. It
consumes only the exact obligations registered by its `ForkInstance`. Multiple
tokens arriving over the same graph edge remain distinct obligations. Readiness,
continuation parentage, canonical ordering, and child retirement follow the fork
and join cohort rules above.

### Independent loop exits

When a child crosses a loop exit, it records a delivered exit payload on the
owning loop instance and retires. It does not immediately resolve the loop's exit
unit outside the loop. Suppressed exits are likewise recorded as causal
resolutions, not emitted independently.

When the final iteration has no live child tokens and no back-edge continuation,
the loop finalizes once:

1. Freeze the accumulated exit set.
2. For each owned exit edge, freeze its delivered collection in canonical order:
   iteration index, fork lineage, child creation ordinal, then token ID.
3. Resolve each owned exit edge exactly once at the outer tag. Its resolution
   carries the complete ordered delivery collection; suppressed edges carry an
   empty collection.
4. Emit one continuation envelope for each delivered distinct exit edge/target.
   Never combine deliveries whose exit edges target different nodes.
5. At a downstream join, expand each edge's delivery collection into labelled
   reducer inputs, wait for one finalized resolution per required inbound edge,
   and invoke that target's `JoinConfig` once. `collect`, `merge`, `reduce`, and
   custom reducers all consume the same canonical ordered sequence; custom
   reducers must therefore be deterministic for that sequence.
6. Complete without delivery when every exit was suppressed.
7. Retire and garbage-collect the completed loop instance after its continuation
   state is durable.

Nested-loop exit ownership continues to use the outermost loop an edge exits. A
crossing token atomically settles its own membership and outcome in every crossed
loop and iteration frame; it does **not** settle an entire crossed instance.
Each instance remains active while it owns other live children or pending
back-edge continuations and finalizes only under its own barrier rules. An edge
that bypasses several enclosing scopes does not cancel remaining inner siblings
unless its declared branch policy is fail-fast/cancel-siblings; otherwise those
siblings continue, and every crossed outer scope waits for their structured
outcomes before it can finalize.

## Checkpoint, replay, and cancellation

A versioned runtime snapshot persists queue envelopes, fork/join instances,
loop/iteration frames, and in-flight dispatch records as one coherent state. Each
snapshot has a monotonic revision and is replaced through repository CAS; a state
transition either publishes one complete next revision or loses the CAS without
partial effects. Replay restores this state directly; it must not reconstruct
tokens from node IDs, visit counters, or partially consumed payload maps.

An in-flight dispatch includes the full token envelope, stable `DispatchId`,
attempt, and idempotency key. A process crash leaves the dispatch in an ambiguous
state. Recovery creates a new attempt for the same token and reuses the stable
idempotency key; external side effects must use that key or remain explicitly
at-least-once. A failed ordinary node can therefore retry without losing payload
or provenance while audit history distinguishes attempts.
Fan-out replay restores the parent/children transition from its durable state and
must never recreate children that were already persisted.

Token scheduling state and loop ownership are separate: a token has exactly one
exclusive scheduling state (queued, executing, join-waiting, or settled) and may
also belong to one nested chain of fork, loop, and iteration owners.

Pause freezes dispatch without settling tokens and preserves every ownership
membership. Graceful stop prevents new top-level work but permits already-owned
tokens to create the continuations required to settle their current structured
scope; it completes after all scopes drain. Cancel prevents new child creation,
settles queued children as cancelled, requests cancellation of executing
children, and applies the declared fail-fast/best-effort policy to their join and
iteration obligations. Nested cancellation settles the inner scope first, then
propagates exactly one cancelled/failed outcome to its parent frame. A stopped or
cancelled snapshot remains replayable and cannot contain an orphan token.

Every cancel transition increments and persists a cancellation generation used
as a dispatch fence. Executing children acknowledge that generation when they
stop. A completion produced by a dispatch from an older generation is rejected;
it cannot satisfy an obligation, create a continuation, or overwrite the already
recorded cancelled settlement. A run becomes terminal-cancelled only after every
executing child has acknowledged cancellation or a newer durable revision has
fenced it and recorded its cancelled settlement.

Crash-after-external-side-effect ambiguity requires the existing idempotency and
side-effect approval contracts; token replay alone does not promise exactly-once
behavior from an external system that lacks an idempotency key.

## Correctness invariants

- Every live token has exactly one exclusive scheduling state: queued, executing,
  waiting in a join, or settled. Its fork/loop ownership chain is non-exclusive
  metadata and contains no cycles or missing parent.
- A `(node, token, attempt)` dispatch happens at most once; retries increment the
  attempt while retaining the stable dispatch idempotency key.
- Fan-out child creation and parent retirement are atomic.
- No loop instance finalizes while it owns a live child token.
- A loop exit unit resolves exactly once per loop instance.
- A join reducer receives exactly the delivered edge-labelled payloads for its
  logical join instance.
- A completed run has no queued envelopes, join buckets, in-flight dispatches,
  live fork/join instances, iteration frames, loop instances, or unsettled tokens.
- Malformed, contradictory, or orphaned persisted token state fails loudly.

## Compatibility and default migration

- Single-token graphs use the same envelope and loop-instance model with one live
  token.
- Existing `JoinConfig` reducers remain the public merge contract.
- Add a settings/runtime schema version. Serializers must preserve explicit
  `False` and explicit `True`; absence remains distinguishable from either value.
- The effective engine mode is resolved when an immutable graph deployment
  snapshot is published and is stored on that snapshot. Upgrades do not silently
  reinterpret an already-published deployment.
- In the default-on release (`v0.12.0`), newly constructed settings and newly
  published legacy records with the field absent resolve to token mode.
- Explicit `sequential_join_enabled=False` selects the legacy path throughout all
  `v0.12.x` releases and emits a structured deprecation warning at validation and
  deployment publication.
- Legacy-path removal occurs no earlier than `v0.13.0` and is a separate change
  with its own compatibility review and migration tooling.
- Round-trip tests cover explicit false, explicit true, absent legacy records,
  and immutable deployment snapshots published before and after the default
  change.

## Verification strategy

Extend the tracked reference oracle and real-runtime trace bridge with events for
token creation, retirement, join consumption, loop ownership, exit accumulation,
loop finalization, cancellation, checkpoint, and replay.

The topology and state matrix includes:

- fan-out branches that all continue, all exit, or mix exit and continue;
- reconvergence at different depths and nested-loop entry/exit;
- multiple children exiting through the same and different exit edges;
- conditional outcomes including none, one, or several active branches;
- disabled forward, back, and exit edges;
- node and branch failure, retry, repository reload, pause, and cancellation;
- checkpoint boundaries before/after fan-out, partial join resolution, child
  exit, loop finalization, and continuation dispatch;
- scheduling permutations with unique edge and token payload fingerprints;
- `collect`, `merge`, `reduce`, and custom reducers.

For every supported trace, verify the invariants above and compare uninterrupted
execution with repository-reloaded replay. Seed every previously discovered
defect class and prove the harness rejects the mutation.

The model-checking grammar is fixed for the release gate:

- `N` is the number of labelled control-flow nodes, including entry and terminal
  nodes and excluding tool edges.
- Valid graphs are reachable, reducible directed multigraphs with one entry,
  declared safeguards for cycles, fan-out width at most three, loop nesting depth
  at most two, at most two iterations per loop, enabled/disabled control edges,
  explicit Boolean condition valuations, and serializable payloads.
- Invalid graphs are limited to irreducible control flow, unsafe unbounded cycles,
  missing required reducer configuration, and malformed/non-serializable state.
  There are zero unsupported shapes inside the valid language.
- At N=4, enumerate every valid labelled topology, enabled mask, and condition
  valuation. Scheduling is exhaustive for states with ready width at most six;
  states exceeding that width cover canonical, reverse, and seeded randomized
  schedules. This gate is therefore topology/state exhaustive and explicitly
  schedule-bounded, not universally schedule-exhaustive. At N=5 and N=6, run a
  deterministic 10,000-case sample per N with recorded seed and shrink every
  failure to a minimal topology/state trace.
- Trace equivalence compares normalized token lineage, edge resolutions, reducer
  inputs/outputs, loop/fork/join lifecycle, terminal output, and persisted state;
  it ignores timestamps, generated database IDs, and audit ordering only where
  the public contract declares ordering irrelevant.

The required artifacts are tracked tests plus a machine-readable JSON report from
the model checker containing grammar version, Git SHA, counts, seeds, mutations,
minimized failures, and separate topology, state, exhaustive-schedule, and
sampled-schedule coverage counts. Release commands are:

```bash
uv run pytest -q
uv run pytest -q tests/orchestrator -m legacy_engine
uv run python scripts/check_token_engine.py --nodes 4 --exhaustive
uv run python scripts/check_token_engine.py --nodes 5 --cases 10000 --seed 120500
uv run python scripts/check_token_engine.py --nodes 6 --cases 10000 --seed 120600
uv run ruff check src/ tests/
```

A HIGH finding is any reachable supported graph that deadlocks, crashes, loses or
duplicates a token/payload/side effect, crosses fork cohorts, violates replay or
cancellation equivalence, corrupts persisted state, or breaks explicit legacy-OFF
compatibility. The final adversarial reviewer must not have implemented the
reviewed task and must review the release SHA without inheriting implementer
reasoning.

## Release gate

The default changes only when all conditions hold:

1. Structured multi-token fan-out and loop-exit semantics are implemented.
2. All oracle, runtime trace, replay, cancellation, and scheduling matrices pass.
3. Every known defect class is represented by a caught seeded mutation.
4. N=4 topology/state-exhaustive, ready-width-six schedule-exhaustive coverage
   and N=5+ deterministic sweeps pass under the stated bounds.
5. The entire project suite passes with the implicit default ON.
6. The explicit legacy-OFF compatibility suite passes.
7. Independent adversarial review reports zero unresolved HIGH findings.
8. Documentation describes the default and temporary escape hatch.

If any gate fails, keep the default OFF and continue remediation. Do not weaken or
exclude a failing correctness assertion merely to flip the default.

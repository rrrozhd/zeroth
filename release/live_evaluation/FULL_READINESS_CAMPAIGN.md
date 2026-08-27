# Zeroth full-readiness campaign

## Decision and claim boundary

The accelerated campaign is authorized for execution under
`accelerated-acceptance-v1.json`. Its maximum incremental exposure is three new
live runs, 17 provider calls, and $1.00. The unchanged runtime ceilings remain
$10.00 tenant-wide and $0.25 per run.

That authorization was consumed by the stopped execution below. The provider-
free remediations were captured in the successor profile,
`accelerated-acceptance-v2.json`. V2 lowered the maximum to 15 calls by replacing
the contradictory three-case/six-call Rightsizing gate with a one-case/four-call
plumbing proof. Its exact authorization phrase is
`AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V2`; that authorization was received and
consumed on 2026-08-26. It did not change V1 evidence.

V2's provider-free preflight is checksum-sealed at
`accelerated-v2-provider-free-preflight-20260826-1`. It passes profile integrity,
both-tenant readiness, the three-embedding Chroma corpus mapping, zero active
provider reservations, and the historical-fixture snapshot. It intentionally
blocks exact W1 serving identity until after authorization, because switching
the primary backend early would disturb the currently inspectable governed-
remediation surface.

V2 then switched only the primary backend to the exact Workflow 1 deployment
and submitted one strict campaign run. Run
`9f121b6f31de4eaca0310419699ef329` succeeded functionally: it retrieved all
three tenant-scoped results, returned the approved source and structured answer,
invoked the controlled tool once, stayed below the run ceiling, and verified a
six-record signed audit chain. The gate nevertheless failed closed because the
public evidence summary reported `conflicting_cost_event`, `incomplete`, and
`$0.00037289`, while the authoritative tenant ledger increased by
`$0.00025859`. The unexplained difference is `$0.00011430`, exactly the first
capability-model call's recorded cost. Workflow 2 and Rightsizing were not run.
The sealed runtime and real-UI evidence roots are
`accelerated-v2-w1-live-grounded-20260826-1` and
`accelerated-v2-w1-ui-failure-20260826-1`. The post-verification UI state is
sealed separately at `accelerated-v2-w1-ui-verified-failure-20260826-1`; it
shows that the signed chain passes while economics remains unreconciled. The
campaign disposition is sealed at `accelerated-v2-closeout-20260826-1`: one new
run, three provider calls, `$0.00025859` incremental ledger spend, zero remaining
active/ambiguous provider reservations, and unchanged historical fixtures.

Passing the accelerated campaign permits only this claim:

> `demo_ready_not_full_campaign_accepted`

It does not convert deferred original criteria to passes and does not constitute
full product-surface acceptance.

## Authoritative baseline

- Campaign: `evaluation-studio-v1`
- Service: `http://127.0.0.1:8122`
- Console: `http://127.0.0.1:3000`
- Current immutable catalog audit:
  `interim-acceptance-gap-audit-20260826-10`
- Baseline catalog result: 123 pass, 0 fail, 15 blocked, 3 not run
- Accepted live Workflow 2 proof:
  `batch-provider-live-closeout-20260826-1`
- Accepted control-plane proof:
  `evaluation-studio-v1-20260826T192455.115634Z-58900ddc923c49ae826c6f911436b4fb`

All named evidence roots are below the external campaign evidence directory and
must remain checksum-sealed. Repository files contain no provider or service
credentials.

## Accelerated acceptance gates

| Gate | Required proof | Maximum new calls | State at authorization |
| --- | --- | ---: | --- |
| Workflow 1 grounded run | Live Chroma retrieval, structured answer and source IDs, signed audit chain, provider usage and reconciled cost | 3 | **Failed; campaign stopped** |
| Workflow 2 repetition 3 | Eight isolated children, concurrency four, ordered join, signed chains, one cost event per call, three-run aggregate reconciliation | 8 | **Not run after stop** |
| Measured Rightsizing | UI submission, one candidate, three recorded cases, persisted quality verdict and cost, refresh restoration | 6 | **Not run after stop; call contract invalid** |
| Audit/economics closeout | Unique operation/run/audit/cost identities, no double counting, reconciled totals and failure tax | 0 | **Blocked by Gate 1** |
| UI persistence | Runs, Economics, and Rightsizing display authoritative values after refresh without reconnecting | 0 | **Runs recovered; full gate blocked by unrun live gates** |

Execution is sequential. A stop condition prevents later gates from running.

## Authorized execution result — 2026-08-26

The accelerated campaign stopped at Workflow 1 and therefore does **not** meet
the demo-readiness claim.

- Run `7c7afe98561a4861a23f6223a7ba8704` made one embedding call and then failed
  before the grounded-agent node.
- The embedding call cost `$0.00000014`; its reservation committed and cleaned
  up, ambiguous exposure is zero, Audit/ledger/Regulus reconcile, and the signed
  chain verifies 3/3.
- The retrieval node recorded `UNMEASURED` with null cost fields even though its
  embedding cost was recorded separately. Strict per-run admission correctly
  refused the next node because the run rollup was incomplete.
- Retrieval returned zero sources, so the semantic-retrieval and grounded-output
  requirements also remain unsatisfied.
- No retry was attempted. The accepted maximum-three-run authorization was not
  widened after failure.
- Workflow 2 repetition three and measured Rightsizing were not executed.

### Authorized V2 execution result — 2026-08-26

V2 also stopped at Workflow 1 and therefore does **not** meet the demo-readiness
claim.

- Run `9f121b6f31de4eaca0310419699ef329` succeeded with the exact structured answer,
  source `evaluation-ground-truth-beta`, three retrieval results, and receipt
  `retrieval-index-live`.
- The model selected and completed the controlled local tool exactly once.
- Three non-cache provider calls were accounted within the gate maximum; the
  tenant ledger increased by `$0.00025859`, well below `$0.25`.
- Audit continuity and keyed signatures verified 6/6. Active and ambiguous
  provider exposure returned to zero.
- Acceptance still failed because the run-evidence rollup reported three cost
  identities but `$0.00037289` total with `conflicting_cost_event` and
  `incomplete`. The UI truthfully renders `$0.000373 needs reconciliation`.
  The `$0.00011430` excess matches one capability-call cost and is unexplained
  double attribution until the rollup implementation is corrected and tested.
- The stop condition prevented Workflow 2 repetition three and Rightsizing from
  executing. No paid retry was attempted.

This result narrows the remaining critical blocker from retrieval/runtime
execution to cost-evidence rollup identity. It is not permissible to relabel the
functional success as an acceptance pass while the economics discrepancy
remains.

### Provider-free cost-rollup remediation

The discrepancy was subsequently fixed without submitting a run or invoking a
provider. The root cause was a source-plane mix: canonical lifecycle audit rows
recorded each of the three provider calls, while the signed `grounded-agent`
runtime projection aggregated both chat turns but carried only the final turn's
`cost_event_id`. The evidence builder grouped both projections under that final
identity and selected the aggregate amount, thereby adding the first chat turn
twice.

`build_summary` now treats `audit_<cost_event_id>` as the canonical one-row-per-
call lifecycle source when present, retains fail-closed conflict detection for
legacy non-lifecycle rows, and normalizes amounts to the ledger's eight-decimal
currency quantum. Three red/green regression cases cover the exact V2 shape,
canonical precedence independent of field placement, and divergent legacy
evidence. The adjacent audit/evidence suites pass 57 tests.

After a bounded backend reload, the immutable source run now reports three
priced calls, three cost events, `$0.00025859`, `correlated`, and `reconciled`.
The signed chain still verifies 6/6. A fresh 1440×900 real-browser inspection
shows `$0.000259 reconciled` and `Cost identities correlated`, with zero console
errors. This supplemental proof is checksum-sealed at
`accelerated-v2-cost-rollup-remediation-20260826-1`.

This remediation does not rewrite the sealed V2 closeout or authorize later
paid gates. It proves that the W1 source run is now functionally and
economically inspectable; Workflow 2 repetition three and the one-case
Rightsizing plumbing experiment remain unexecuted and require a new bounded
authorization.

### Accelerated V3 successor boundary (prepared, unarmed)

The smallest honest successor is versioned at
`accelerated-acceptance-v3.json`. It permits exactly two new live runs and at
most 12 provider calls: one eight-child Workflow 2 parent for repetition three,
then one one-case Rightsizing experiment whose measured endpoint performs four
calls. It retains the `$1.00` incremental, `$10.00` tenant, and `$0.25` per-run
ceilings. Its exact, currently **unreceived** phrase is
`AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V3`.

The W2 driver is fail-closed around immutable history. Before it can submit a
parent, it verifies both the sealed W1 cost-rollup remediation and
`batch-provider-live-closeout-20260826-1`, which proves successful repetitions
one and two and 16 reconciled child calls. It then constructs only repetition
three, with eight items and concurrency four, and seals its own result. It
cannot invoke the historical three-parent/24-call plan.

Rightsizing no longer relies on the old driver's implicit three-case contract.
The V3 contract is explicit: target node `analyze`, `max_cases=1`,
`min_cases=5`, exactly four captured provider calls, and verdict `flagged`.
This demonstrates real interface/runtime/persistence/economics plumbing only;
it cannot be reported as confirmed model-quality equivalence. Shared-project
usage remains blocked by the provider's 403 permission response and is not
fabricated as zero. That external cross-check remains outside any V3 pass.

The provider-permission state is represented explicitly as
`unavailable_campaign_local_only`: its unavailable window must carry zero and
is never compared or displayed as provider-project usage, while Audit,
reservations, local Economics, and Regulus still reconcile exactly. A usable
shared-project window continues to use `upper_bound_only`. The V3 profile is
frozen at SHA-256
`14ed4b120d06b8e7a3af8ca18d9659ffba62d93e210be9d5dc796227fb83c270`;
the prepared external wiring targets `analyze`, one case, and the explicit 403
state. No provider or run call was made while preparing it. Twenty-seven focused
V3/Rightsizing tests and 67 adjacent batch/collector/adapter tests pass.

Primary evidence is sealed at
`accelerated-w1-live-grounded-failed-20260826-1`; its cost diagnostic is sealed
at `accelerated-w1-live-grounded-failed-20260826-1-diagnostic`. The real Runs UI
failure state is sealed separately at `accelerated-w1-failed-ui-20260826-1`.
That checkpoint shows the persisted failure and visit counts, but also records
that attributed cost, scope, lineage, timeline, evidence, and input-contract
details were unavailable or returned fetch errors. Its raw accessibility
snapshot was rejected by the secret scanner and was not retained. After Redis
and both backends became ready, every affected endpoint returned 200 and a fresh
real-browser load restored tenant scope, role, `$0.00000014` attributed cost,
timestamps, node timeline, embedding cost, and the generated input-contract
example. That recovery is checksum-sealed at
`accelerated-w1-failed-ui-recovered-20260826-1`; it corrects the presentation
discrepancy but does not alter the failed Workflow 1 result.

The transport diagnosis found no data, authorization, or route defect. During
the original browser attempt, CORS preflights returned 200 after a backend
replacement, but several corresponding GETs never reached FastAPI; other GETs
completed normally. A fresh WebKit load later produced all 200 responses and no
console or fetch error. The resilience fix now retries a thrown transport-level
GET once after a 100 ms backoff under the original timeout. It never retries a
mutation, received HTTP error, or caller-aborted request. Thirty focused API,
Runs, and Topbar frontend tests and TypeScript pass. A fresh controlled-browser
load restored every required read-only panel without a visible fetch error; the
checksum-sealed proof is `accelerated-w1-ui-transport-recovery-20260826-1`.

Workflow 2's provider-free preflight is sealed at
`accelerated-w2-happy3-preflight-20260826-1`. At capture time it recorded an
incorrect serving identity, unhealthy Redis readiness, and the active W1 stop
condition. Budget admission itself had sufficient headroom and there was no
active or ambiguous exposure. Redis has since been added as a persistent Docker
service and both tenant backends now report ready, but that post-failure repair
does not retroactively pass the gate.

Rightsizing performed zero calls. Its preflight found an independent contract
contradiction: the current measured endpoint makes four calls per case, so the
specified three cases require 12 calls, exceeding the accelerated six-call gate.
The pinned node also has no eligible successful recorded cases, and the shared
provider-window artifact remains a permission-blocked 403 rather than a usable
window total.

### Remediation before another live authorization

1. **Implemented and provider-free tested:** retrieval now promotes a settled
   embedding cost into retrieval history exactly once. Missing or invalid
   settlement remains unmeasured and fail-closed. The focused and adjacent
   suites pass 117 tests; a paid rerun has not yet proved the fix live.
2. **Corrected, live query pending:** the persisted Chroma connector now uses
   the seeded `zeroth_memory` collection prefix, whose accepted corpus contains
   three documents. No paid embedding query was used to manufacture proof.
3. **Partially complete:** persistent Redis was added to the Docker stack and
   both tenant readiness endpoints report `ok`. The primary service currently
   serves `evaluation-studio-v1-governed-remediation-v2` at graph
   `evaluation-studio-v1-governed-remediation@6`, so the exact Workflow 1 or 2
   deployment still must be selected before its gate.
4. **Persistence implemented; live scope decision unresolved:** the present
   Rightsizing endpoint makes four calls per case. The full sanitized completed
   report is now stored in the existing non-dispatchable experiment run and
   restored through a tenant/workspace/deployment-scoped read endpoint after UI
   refresh. Either authorize 12 calls for three cases, or reduce the next
   accelerated proof to one case and four calls. The latter is the bounded demo
   option; neither may be silently represented as the original six-call gate.
5. **External limitation unresolved:** provide a valid provider-window
   cross-check or keep that original criterion explicitly blocked by provider
   permissions. The existing 403 is not evidence of zero usage.
6. **Authorization required:** obtain a new bounded authorization before any
   paid rerun. The stopped campaign and all sealed failure evidence remain
   immutable.

### Historical fixture-state boundary

The persistent tenant contains three lease-free `RUNNING` batch children,
thirteen `IN_FLIGHT` negative-fixture operations, and three `AMBIGUOUS`
synthetic action/timeout operations. They belong only to
`evaluation-studio-v1`, have no provider request, reservation, or cost-event
identity, and total zero provider cost. They are retained negative-case evidence,
not current leased work.

They must not be silently reconciled or directly edited before another gate.
The public operator-resolution API accepts only authoritative `AMBIGUOUS`
outcomes, does not resume runs, and writes the currently served deployment/graph
into its signed audit. Some retained fixtures originated on superseded graphs,
so resolving them now would create misleading evidence.

The next live bundle must instead snapshot their exact identities before and
after execution, prove that they remain unchanged and economically inert, scope
acceptance to the newly tagged run/child/operation/cost identities, and label the
historical fixtures as exclusions. It must not claim tenant-wide zero ambiguous
or in-flight state.

## Stop conditions

- Secret, authorization header, or signing material appears in evidence.
- A required audit chain is broken or unsigned.
- A provider outcome or reservation is ambiguous.
- Tenant-scoped data crosses a tenant boundary.
- Audit, local Economics, and Regulus totals differ beyond the configured
  tolerance.
- Any runtime, per-run, incremental, call-count, or tenant ceiling is exceeded.
- A composed parent or lifecycle audit row is counted as a second provider call.

## Original full-readiness work remaining at authorization

The following items remain outside the accelerated claim unless the new evidence
directly satisfies an eligible criterion. They must not be silently relabeled.

### Workflow 1

- Three successful live repetitions are required for full campaign acceptance.
  The accelerated campaign executes only repetition one.
- Live semantic retrieval, the structured answer/source contract, final output,
  signed audit chain, and economics must be correlated in the accepted run.

### Workflow 2

- Repetition three and the three-repetition aggregate-economics gate remain.
  The first two repetitions are already accepted.

### Campaign-wide economics and audit

- Exactly one cost event for every non-cache provider call across the complete
  original live matrix.
- Audit, local Economics, and Regulus totals reconciled campaign-wide.
- Shared-provider-window cross-check. The OpenAI organization usage endpoint
  currently returns 403 for the project credential, so this remains an explicit
  external limitation rather than a fabricated zero.
- Measured or estimated failure tax displayed and accepted.
- Complete valid campaign audit and proof of no economic double counting.
- Zeroth Check only after all original workflow gates pass.

## Acceptance evidence contract

Each accelerated gate requires all of the following; no screenshot alone passes
a criterion:

1. A configured-state screenshot from the real UI.
2. A result screenshot and a refresh-restored screenshot.
3. Sanitized runtime request/result metadata.
4. Exact run, operation, audit, reservation, cost-event, and provider-request
   identities when available.
5. Signed-chain verification.
6. Local Economics and Regulus reconciliation.
7. A recursive secret scan and a complete SHA-256 manifest.

## Completion outcomes

- **Accelerated pass:** all five accelerated gates pass and the sealed report may
  state `demo_ready_not_full_campaign_accepted`.
- **Accelerated fail:** a gate contradicts its required result; subsequent gates
  stop and the discrepancy is preserved.
- **Accelerated blocked:** an external condition prevents evidence collection
  without contradicting the product; the remaining gates stay unrun.
- **Full readiness:** only a later audit that proves every original catalog item
  can claim full campaign acceptance.

## Adversarial review

The strongest objection is that one Workflow 1 run and a three-case Rightsizing
experiment are too small to support reliability or model-quality generalization.
That objection is correct. The accelerated campaign proves the end-to-end demo
paths and accounting invariants, not statistical production readiness. Provider
instability, a shared-project usage 403, and UI/browser state can still block the
campaign. The simpler fallback is to preserve the existing 123-pass sealed
baseline and report the exact failing or blocked accelerated gate without making
a demo-readiness claim.

## Operator handoff

Keep the persistent Docker service and external volumes intact for inspection.
Rollback is operational rather than destructive: stop new runs, retain the
append-only evidence roots, restore the last accepted deployment version, and
restart the service. Never delete campaign tenants or evidence during rollback.
Credential rotation occurs only after the final provider call and does not alter
the accepted evidence, which contains identities and hashes rather than keys.

## Accelerated V3 execution result — 2026-08-26

V3 authorization was received and consumed. The bounded execution completed
both paid gates without exceeding its two-run, 12-call, `$1.00` incremental,
`$0.25` per-run, or `$10.00` tenant ceilings:

- Workflow 2 repetition three completed as parent run
  `d2d32e9bbcc3400086206d283116441b`, with eight ordered child calls,
  configured and observed concurrency four, and `$0.00097695` incremental
  cost. Its immutable bundle is
  `accelerated-v3-w2-repetition3-20260826-1`.
- The one-case measured Rightsizing experiment completed as run
  `rightsizing:31624325c9ce48f7a2af99e02e020174`. It made exactly four
  non-cache provider calls, recommended `openai/gpt-5-nano`, reported 53.6%
  projected savings, and correctly returned `flagged` because one case is
  below `min_cases=5`. Audit, reservations, local Economics, and Regulus each
  reconcile to `$0.00051700`; all four cleanup states are complete and no
  active or ambiguous reservation remains. The provider adapter did not expose
  request IDs, so the calls join through their exact operation, cost-event,
  runtime-audit, run, tenant, campaign, and cleanup identities. The immutable
  bundle is `accelerated-v3-rightsizing-one-case-20260826-1`.

The first Rightsizing wrapper invocation stopped before any provider call
because signed audit records intentionally omit replay snapshots. That
zero-call discrepancy remains preserved at
`accelerated-v3-rightsizing-zero-call-stop-20260826-1`. Durable run-history
snapshots are now joined onto in-memory audit identities without rewriting the
signed chain. The successful product run then exposed three evidence-adapter
defects: provider request IDs were incorrectly mandatory, the audit verifier
used the wrong HTTP method/path, and lifecycle/runtime audit projections were
treated as duplicate calls. All three now fail closed on actual ambiguity while
accepting the product's documented optional-provider-ID behavior and canonical
runtime projection.

Rightsizing opportunity identity is now canonical. Runtime branch/subgraph
prefixes aggregate into authored node `analyze`, while source deployment
identity prevents different child deployments from being conflated. The live
UI currently reports 24 source calls and `$0.002894` estimated spend for
`analyze`; the measured experiment's own four calls remain separately visible
as `rightsizing:analyze` rather than being silently mixed into source history.

The real browser refresh initially failed because CORS allowed `X-API-Key` but
not the required `X-Tenant-ID` scope header. The allowlist now includes the
tenant header and has a regression test. After backend and frontend recreation,
the connected Rightsizing page restores tenant-wide/platform-admin scope, the
persisted `flagged` result, all four live-call rows, `$0.000517` estimated cost,
complete cleanup, current opportunities, unit economics, and waste. The
browser console is clean and the opportunity region has no horizontal overflow
at the captured desktop layout. UI evidence is sealed at
`accelerated-v3-rightsizing-ui-20260826-1`.

Focused verification after these fixes is green: 35 authoritative
export/Rightsizing reconciliation tests, the CORS regression, 40 backend
Rightsizing/opportunity tests, and 31 focused frontend tests. The persistent
Docker service continues to serve
`live-provider-batch-economics-20260826-2-parent`.

### V3 claim boundary

The accelerated result is **`demo_ready_not_full_campaign_accepted`**. It proves
Workflow 2's third repetition, the bounded measured Rightsizing plumbing path,
exact local cost-plane reconciliation, refresh persistence, and a working live
operator surface. It does not prove statistical model-quality equivalence,
because the experiment intentionally ran one case; it does not satisfy the
shared-project usage cross-check, because the provider endpoint remains 403;
and it does not retroactively supply Workflow 1 happy repetitions two and three
or every negative/field/browser matrix item in the original catalog.

The strongest objection is that the live demo can be mistaken for full product
readiness. That objection is valid: the visible recommendation is a candidate
for further testing, not a production switch decision. The safer operating
choice is to keep the persistent instance for inspection, rotate the disposable
credential after inspection, and schedule the remaining full-campaign matrix as
a separate bounded campaign rather than extending this consumed authorization.

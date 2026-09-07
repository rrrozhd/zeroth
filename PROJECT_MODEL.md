# Zeroth product and implementation model

## Purpose and scope

Zeroth preserves its open-source workflow runtime, SDK and console. The hosted
Solo product adds economic evidence ingestion, version comparisons, bounded
model experiments, retained history and scheduled comparisons for applications
which continue running in their customer's environment. The application owner
defines business outcomes and controls rollout.

The authoritative private acceptance contract is
`PROGRESS.md`. Every product gate is open as of 2026-09-06. This file explains
the implementation; it does not duplicate or weaken those acceptance gates.

## Critical flows and invariants

- Lean SDK: `packaging/sdk/src/zeroth/protocol/models.py` defines wire events;
  `packaging/sdk/src/zeroth/sdk/client.py` sends authenticated HTTP requests.
  Its distribution must not include the runtime, economic plane or console.
- Ingestion: `src/zeroth/econ/plane/cloud/api.py` owns hosted routes; scoped
  storage binds evidence to authenticated tenant/workflow/version/run identity.
  Exact delivery retries cannot double-charge. Changed replay must conflict.
  The shared immutable execution comparison includes deployment, production versus
  synthetic classification, and cost/usage provenance as well as values. Both
  ordinary retries and database-conflict recovery use that comparison.
  Outcome retries likewise require identical value, typed payload, provenance,
  linkage and asserted timestamps. Conflicting historical duplicate rows are
  rejected without deletion; receipt time and row IDs do not change identity.
  `instrumentation/identity.py` separates public workflow/version/run identity
  from globally keyed legacy registries. New SDK registries hash the bound tenant
  and public names; an existing, owned legacy SDK mapping is preserved. Registry
  display names remain readable. Outcomes retain nullable public workflow fields.
  Decisions, debugger and provider allocation share the outcome join: a run ID
  alone cannot identify a workflow/version. Ambiguous legacy mappings stay unresolved.
- Decisions: `src/zeroth/econ/plane/decisioning/service.py` joins stored
  evidence; `src/zeroth/econ/decisioning.py` applies policy and retains reasons.
  Unknown cost/outcome evidence must not become measured completeness.
  Thresholds use exact ratios of counts and decimal totals; display rounding
  must never decide a verdict. Zero baseline cost leaves relative change undefined.
  Comparisons and schedules require an explicit quality floor. Current reports
  retain an observed claim class, method version and limitations; policy passes
  recommend review. Historical reports retain legacy labels and actions.
  Hosted comparison reports also retain `source_evidence` fingerprints and counts
  of the selected immutable execution/outcome assertions. Equal headline totals
  cannot collapse changed selected inputs into the same retained revision. This
  reuses the loaded rows and existing report JSON; it does not copy raw payloads,
  prove source completeness, or establish a frozen time window. Old reports keep
  an empty source binding. Debug a changed revision from its source counts/digest
  before comparing totals; original source rows are still needed to audit assertions.
  New comparisons also retain calculation_inputs (run-economics/1) in the existing
  report JSON. decisioning.py groups exact normalized cost/outcome tuples with their
  run multiplicity, without source IDs or raw payloads. Existing report digests bind
  the rows. These frozen inputs reproduce VersionEconomics and policy arithmetic
  after late evidence or source erasure; they do not prove source normalization,
  complete delivery or an atomic database snapshot. Old reports keep null inputs.
  Debug arithmetic from retained rows/policy; debug evidence meaning from the
  independent source ledger, definitions and source fingerprints. Source erasure
  preserves aggregate report/calculation history under existing report retention.
  No new schema or endpoint is needed. Rollback readers must accept the optional
  calculation_inputs field on new records; no rollback image is certified.
  Optional `source_windows` on comparison requests reconcile caller-owned run
  inventories against received executions selected by tenant, workflow, version
  and immutable `source_window_id`. The SDK helper hashes unique execution IDs in
  UTF-8 byte order; the inventory must originate in the producer before delivery.
  Missing runs enter the denominator and incomplete run costs become unknown.
  A delivery mismatch or scan overflow forces abstention in the shared domain
  decision rule. `source_delivery` retains digests and counts, not raw run lists;
  altered inventories yield new report revisions through existing retention.
  Debug from its missing/unexpected/mismatched counts, then compare the caller's
  ledger with source execution rows. No new manifest endpoint/store, quota,
  dependency or queue exists. Schedules still read observed history without an
  inventory binding. Technical closure never sets a business outcome or proves
  provider charge completeness; source and outcome maturity remain separate.
  Monetary assertions can now declare `cost_role=charge` and tenant-wide charge_id;
  a database unique index and the existing rollback/re-query path permit one
  execution owner, including races. A different owner conflicts rather than being
  called a duplicate. Summaries are non-monetary and never feed inferred pricing.
  Summary-only runs remain unknown-cost; legacy amounts keep unverified ownership.
  Version reports retain charge counts/status, while debugger summary_events
  separates structural spans from monetary event counts. Investigate a charge
  conflict from the caller's independent provider/account/attempt identity and
  its existing scoped execution owner. Capture primary_for_rollup metadata is
  not a monetary selector. The execution remains the sole physical owner.
  Charge-cost revisions now append in charge_cost_revisions, referencing that
  owner. The cloud POST/GET paths and SDK contract use existing roles and event
  metering. instrumentation/charge_costs.py resolves the latest non-future source
  assertion into immutable CostAmounts; decisions, debugger, provider allocation
  and legacy cost readers use it without changing execution rows or attempt counts.
  Replacements cover all three cost components and provenance. Zero refunds the
  amount; unmeasured withdraws it. Cost stays attributed to the original run/time.
  Source time must follow the execution; source clock validity remains unverified.
  Replays compare complete assertions, including reason, under a unique tenant/
  charge/time constraint. SQLite's post-flush owner check prevents erasure from
  leaving orphan revisions even when FK enforcement is off. Erasure deletes and
  counts revisions before owners in the existing transaction. Source fingerprints
  bind revisions as v5; execution-inventory counts still measure execution delivery
  only. Debug from the original owner, revision history, source time and measurement.
  Outcome interpretation uses the existing immutable workflow-version definition
  in debugger/service.py for decisions, debugger and provider allocation. Stored
  decisions abstain on missing/type-mismatched or incompatible rules and retain
  definition/rule digests in outcome_semantics. The existing full-report digest
  binds these declarations. Diagnose
  missing labels from the exact tenant/workflow/version definition and the latest
  typed observation; an invalid latest value cannot revive an older success.
  Admin cloud keys/sessions can use the existing outcome-definition POST path;
  the SDK exposes that same request. Other legacy authentication stays unchanged.
  Maturity is separate from both the rule and provenance. One nullable outcome
  column preserves unknown legacy history. SDK/runtime/API declarations allow
  unknown, provisional, final and withdrawn; final requires an observation and
  withdrawn forbids one. The shared join excludes future source assertions, then
  selects the latest matching type before applying finality and the typed rule.
  A newer provisional/withdrawn state cannot revive an earlier success. Revisions
  append using a later aware source assertion timestamp; preserve the timestamp
  for retries, and original business-event time separately when it differs.
  Query responses expose maturity for diagnosis. Non-unknown declarations bind
  stored-assertions/4 fingerprints; old v1–v3 bytes remain stable. Rollback readers
  must understand observed-policy/3 semantics to recompute decisions; old reports
  retain their original interpretation. Producer clock correctness, independent
  business truth and complete historical windows remain unverified.
- Experiments: `src/zeroth/econ/plane/backtesting/` owns bounded execution,
  reservation/metering and retained results; analytics owns model evaluation.
  Hosted replays reuse the correctness evaluator, then price captured input/output
  usage separately for incumbent, candidate and judge using retained rate
  snapshots. `_HostedUsageMeter` counts actual adapter invocations and unresolved
  usage; it cannot see internal provider retries. Missing usage abstains. The
  legacy OSS experiment path remains separate. Provider experiment cost and
  projected customer workload savings are distinct.
- Identity and money: WorkOS organization identity owns the tenant; verified
  Paddle webhooks own paid access. A redirect cannot grant entitlement.
- Operations: `src/zeroth/econ/plane/main.py` mounts the standalone service;
  `decisioning/scheduler.py` runs tenant-scoped due scans in one launch replica.
  Health returns 503 for noncurrent schema or missing/finished enabled scheduler.
  A live task is not proof of successful recent work.

## Decisions and alternatives

Selected: Solo $39/month, monthly only, 14-day trial, WorkOS AuthKit, Paddle,
Railway with managed Postgres, no Team sale, preserve OSS. The standalone cloud
image applies only the economic migration chain (`alembic_version_econ`).

D01–D03 were approved by the owner in the private `DECISIONS.md`: additive
evidence contracts, exploratory paid claims, optional pinned hook recipes.
Avoid a second ledger, per-framework economic engines or a new statistics model
as workarounds for missing evidence. R2 still requires C01–C07.

## Testing, debugging and operation

Begin with the SDK request and authenticated tenant, then stored event/replay
identity, joined evidence, rule reasons, retained result and scheduler state.
Use `tests/sdk`, `tests/econ`, `tests/econ_plane` and the applicable
`tests/release_gates` checks. Freeze acceptance workloads before measuring.

This checkout starts at main `9a9cd67d` on `acceptance/paid-product`. Tests can
use the existing release-checkout environment with explicit `PYTHONPATH=src`;
SDK-specific tests explicitly load their packaged source. Record imported source
paths to avoid testing the old checkout accidentally.

Deploy/restore/billing procedures and existing release tools live in
`docs/operations/hosted-commerce.md`, `cloud-launch-evidence.md`,
`cloud-launch-policy-inputs.md`, and `release/cloud_*`. No deployment, migration,
publication, purchase or external account change has been performed here.
The readiness repair has no schema change: revert its code and documentation
to roll it back, recognizing that the prior HTTP behavior hides failed readiness.
Hosted replay-cost reports add fields inside retained JSON. Before deployment,
prove the selected rollback image can read that report format; older strict
readers are not automatically compatible. No production records have been written.
Economic migration `20260906_18` adds outcome workflow columns and their tenant
lookup index without backfilling or rewriting history. Apply the economic chain
before running the new service against PostgreSQL; startup refuses missing columns.
SQLite compatibility uses the same migration. After new outcomes exist, downgrade
refuses to discard their public identity. A rollback build must retain these columns
and the compatible reader; local mixed-history tests do not certify a rollback image.

Migration `20260906_19` adds nullable execution `source_window_id` and its tenant
index. Apply the economic migration chain before starting PostgreSQL replicas;
SQLite compatibility uses the same migration. Old records remain unwindowed.
Downgrade refuses to discard populated window identities. A rollback image must
understand `source_delivery` and `stored-assertions/2` reports and retain the new
column. Existing source-row erasure removes window IDs with those rows; the
retained digests cannot reconstruct erased source evidence or the caller's ledger.

Migration `20260906_20` adds nullable execution cost_role/charge_id and the unique
tenant/charge index. NULL historical roles mean legacy_unknown; no ownership is
backfilled. It refuses to discard populated new declarations on downgrade. A
rollback build must retain these columns and read stored-assertions/3 plus charge
ownership reports. Single ownership holds among retained records: source erasure
removes the owner with the execution. Corrections/repricing use the append-only
revision contract; do not work around immutability with a new charge ID.

Migration `20260906_21` adds nullable outcome maturity without backfill. Apply the
economic chain before starting PostgreSQL replicas; startup checks the column,
and SQLite compatibility uses the same migration. Downgrade refuses to discard
non-unknown declarations. Roll back with a compatible reader that preserves the
column and understands observed-policy/3 and stored-assertions/4. Query the latest
source timestamp, maturity, typed value and workflow definition when a label is
unresolved. These local checks do not certify a rollback image or producer truth.

Migration `20260906_22` adds the charge-cost revision table, owner FK and unique
source assertion identity. Nonempty revisions prevent downgrade; a compatible
reader rollback must keep the table and v5 source interpretation. PostgreSQL uses
Numeric(18,8); SQLite revision amounts use decimal text because numeric affinity
rounds valid large amounts and breaks exact retries. The private ORM amount type
returns Decimal for both. Use the shared resolver rather than SQL float coercion
when aggregating these amounts. This does not certify historical SQLite execution
amount precision, fixed source snapshots or an assembled rollback image.
Fractional revision amounts require decimal strings/Decimal at the SDK/API boundary;
float input is rejected before it can silently become a rounded assertion. Integral
JSON numbers fit the declared range exactly. Bootstrap creates its legacy tables,
checks/converges migrated parent columns, then creates the dependent revision table.
The migration-only topology can omit the runtime execution table; revision creation
waits for bootstrap there. This preserves existing offline FK installation rules.

Migration `20260906_23` extends exact SQLite storage to original execution amounts,
reusing the private ORM cost type. All original protocol mirrors enforce the existing
18,8 monetary range; owned charges require decimal strings/Decimal or integers.
Representable legacy floats remain compatible. PostgreSQL keeps Numeric(18,8).
SQLite migration reads historical amounts through the old Numeric(18,8) result
processor and stages exact text in batches of 1000 during the table rebuild. Raw
SQLite CAST/printf would change historical assertions and must not replace it.
Already-lost digits are not recoverable. IDs, charge ownership, child revisions and
indexes stay intact; the connection-local staging table is removed after conversion.

Stop application writes before the SQLite upgrade. The migration requires an offline
connection with foreign_keys=OFF and refuses before rebuilding when enforcement is
on; otherwise dropping/recreating the parent could delete child revisions. Run the
packaged economic migration on that connection, then verify PRAGMA foreign_key_check
returns no rows before enabling enforcement and starting compatible readers. Fresh
exact-text tables need no rebuild. PostgreSQL follows the normal economic migration
chain. A downgrade with any retained monetary amount is refused. Rollback readers
must understand exact SQLite text and existing report contracts; no assembled
rollback image is certified. Unknown amounts and original source-truth limitations
remain unchanged.

Budget status and admission stream the original amount columns through the scoped
reader and sum Decimals before applying their existing policy. SQLite SQL arithmetic
would coerce the exact text back to binary floats and can wrongly reject a request
at its ceiling. Reservation/cap storage, event inclusion rules and response types
are unchanged; their broader acceptance remains open. Application-side scanning is
bounded in memory, but capacity and lock duration still require A11 measurements.
The execution constructors expose their actual validated fields and unknown-cost
defaults. Approved D01/D02 signature changes are recorded in
`docs/backend-import-migration.md`; the immutable legacy fixture is preserved.

## Current risks and unfinished work

The SDK source-ledger check runs through real local HTTP and compares source
integer amounts with stored costs, decisions, failed-run exposure and retry spend.
New SDK writes map their attempt into the stored field; older collapsed attempts
stay immutable and their historical retry breakdowns remain unverified. Debug
delivery in `cloud/api.py`, then `instrumentation/service.py`: a same-execution
winner between identity and charge checks proceeds to full duplicate/conflict
reconciliation. Different charge owners still fail. The SDK has no internal retry
queue; callers retain source payloads and replay stable identities after uncertain
delivery. This proves the bounded synthetic ledger, not provider capture or invoices.

Missing-cost defaults now preserve unknown values; closure/ownership and
unvalidated confidence still require acceptance work. Hosted projections now use
observed replay usage; cache/discount/tool/internal-retry costs are not certified.
Backtest
decisions require an explicit quality floor before execution and after computation.
Analytical normal quantiles and Wilson count validation are repaired; this does
not validate legacy Bayesian labels, bootstrap gates or population claims.
Scheduler freshness, spend and
recovery bounds, compatibility live-provider tests, five unfamiliar-user sessions,
actual vendor transactions and independent repeat use/renewal remain unproven.
Historical passing tests do not establish current-candidate product acceptance.

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
  before comparing totals; original source rows are still needed to reconstruct it.
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

## Current risks and unfinished work

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

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
- Decisions: `src/zeroth/econ/plane/decisioning/service.py` joins stored
  evidence; `src/zeroth/econ/decisioning.py` applies policy and retains reasons.
  Unknown cost/outcome evidence must not become measured completeness.
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

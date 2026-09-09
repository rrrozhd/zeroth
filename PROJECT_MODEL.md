# Zeroth Project Model

## Purpose and boundary

Zeroth is a governed execution and economic decision platform for production AI workflows. Its
runtime orchestrates graphs, agents, tools, memory, approvals, and executable units under explicit
policy and audit boundaries. Its economic layer captures versioned execution, cost, latency,
outcome, and provenance evidence, then turns that evidence into inspectable change recommendations.

The first probabilistic decision concerns moving a workload from an incumbent model
to a candidate. Currently every public forecast remains experimental and abstains;
its action distributions are diagnostics, not permission to ship or reroute.
An unqualified risk law yields no action distributions at all.
Zeroth does not modify customer routing.

The hosted Solo offer uses that evidence layer for applications that remain in the
customer's runtime. Customers define business outcomes and control rollout.
The observed-evidence contract and experimental forecast safeguards apply to
both the hosted service and self-hosted platform.

## Relevant architecture

- `src/zeroth/econ/probabilistic.py` owns the transport-independent calibration, paired
  bootstrap, Monte Carlo, chance-constraint, empirical VaR/CVaR, sensitivity-breakpoint, and
  abstention semantics.
- `src/zeroth/econ/rollout_verification.py` owns sticky randomized assignment and conservative
  bootstrap treatment-effect verification.
- `src/zeroth/econ/plane/decisioning/` owns authenticated Cloud requests, tenant isolation,
  fresh evidence harvesting, immutable history, schedules, rollouts, and recalibration records.
- `src/zeroth/econ/plane/reports/` renders one byte-stable PDF per decision/template version,
  serves it through tenant-authenticated APIs, and audits explicit SMTP delivery attempts.
- `packaging/sdk/src/zeroth/protocol/` duplicates the lean transport contracts without importing
  the server runtime.
- `packaging/sdk/src/zeroth/sdk/client.py` sends typed requests and reads retained history.
- Alembic revisions `20260901_18`, `20260902_19`, and `20260902_20` own the decision,
  closed-loop, report-artifact, and delivery-audit tables.

## Distribution boundaries

- `zeroth-platform` is the full self-hosted distribution and provides the `zeroth` CLI.
- `zeroth-core` is an exact-version compatibility package; `zeroth-core` remains a temporary CLI
  alias. Neither compatibility path owns implementation.
- `zeroth-sdk` is the lightweight remote Python client. Its experimental client version is `0.1.0a1` and
  requires an explicit `base_url` while no supported public Cloud endpoint exists.
- `zeroth-console` is an optional static operator interface, not the product's enforcement or
  decision boundary.
- All Python imports remain in the shared PEP 420 `zeroth.*` namespace.

## Critical flow

1. A customer supplies paired evidence or a durable selector. Cloud harvests measured run-level
   telemetry or an explicit case-level backtest artifact; aggregate-only evidence abstains.
2. Cloud derives per-metric calibration and drift state for cost, success rate, p95 latency, and
   critical-error rate; it does not trust a caller-provided readiness claim. The weakest or
   missing metric controls overall readiness.
3. The domain engine requires explicit monthly demand and measured critical outcomes,
   matching paired IDs, calibration, drift, paired sample size, and the rare-error upper
   bound. A failed gate returns `collect_evidence` without running scenarios.
4. Each scenario resamples n complete paired runs, samples monthly demand D, draws D
   whole future paired requests from that outer empirical law, and uses common routing
   uniforms across actions. Sparse integer counts aggregate spend, success, critical
   errors and exact p95 latency without allocating D-row populations.
5. The private diagnostic preserves point feasibility and separately computes fixed-N
   99% simultaneous Hoeffding bounds for 3*A breach probabilities. A point-feasible
   saving action cannot become the sampled diagnostic recommendation unless its
   probability bounds qualify. These bounds do not certify CVaR or predictive validity.
   The public wrapper retains diagnostics but returns abstain/collect_evidence,
   even when callers disable calibration requirements. Service storage retains this lineage.
6. A schedule persists the selector and policy and rebuilds evidence on every due run.
7. A randomized rollout persists sticky subject assignments. Post-assignment measured outcomes
   produce conservative causal effects; contamination invalidates the causal label.
8. Verified rollout observations become calibration history for later scheduled forecasts.
9. An authorized user may render a retained decision once, download the stored bytes, and deliver
   that exact SHA-256-bound artifact by authenticated link or email attachment.

## Invariants

- A critical calibration/drift state cannot produce a migration recommendation.
- Estimated scenario count never substitutes for real evidence count.
- Incumbent and candidate samples are paired by stable case identity.
- Complete observations are resampled so cost, latency, quality, and failure dependence remains
  intact.
- Tighter risk limits cannot enlarge the feasible action set.
- An infeasible action is never recommended.
- Money stays decimal-safe on the API boundary.
- Evidence and retained history are tenant scoped.
- Persistence records numeric evidence and provenance, not prompts or raw model content.
- Repeated requests with the same tenant, evidence, policy, calibration history, seed, and
  simulation count are idempotent.
- A scheduled run never replays a retained evidence snapshot.
- Aggregate backtest rates are never converted into synthetic paired observations.
- Any model-assignment noncompliance invalidates causal verification.
- API metadata, downloaded PDF bytes, and every delivery audit row identify the same report hash.
- SMTP credentials come from deployment settings and are never accepted in report API payloads.

## Decisions and rejected alternatives

- Use a paired empirical bootstrap first, rather than a universal parametric or deep
  probabilistic model. It is easier to inspect and preserves observed dependence.
- Enumerate inspectable global or cohort routing actions rather than hiding a continuous solver.
- Let customers own risk thresholds; Zeroth supplies mechanics and conservative defaults.
- Derive Cloud readiness from calibration observations rather than accepting a readiness flag.
- Use the approximate zero-event `3/n` upper bound as an evidence gate for rare critical errors.
- Causal claims require server-retained random assignment before execution; before/after and
  observational comparisons remain non-causal.

## Testing and debugging

- Domain: `uv run pytest tests/econ/test_probabilistic_decisioning.py
  tests/econ/test_forecast_calibration.py -q`
- API and persistence: `uv run pytest tests/econ_plane/test_probabilistic_decision_api.py -q`
- Closed loop: `uv run pytest tests/econ_plane/test_probabilistic_evidence_harvesting.py
  tests/econ_plane/test_probabilistic_decision_schedules.py
  tests/econ_plane/test_randomized_rollout_service.py -q`
- Migration: `uv run pytest tests/econ_plane/test_probabilistic_decision_migration.py -q`
- SDK path: `uv run pytest
  tests/sdk/test_cloud_e2e.py::test_sdk_submits_and_reads_a_probabilistic_model_migration_decision
  -q`
- Reports: `uv run pytest tests/econ_plane/test_decision_reports.py
  tests/econ_plane/test_decision_report_migration.py -q`
- Debug a retained report from `probabilistic_migration_decisions.report_json`, then compare its
  `request_digest`, evidence snapshot, policy, seed, and calibration state.

## Evidence ingestion and observed decisions

- SDK wire models in `packaging/sdk/src/zeroth/protocol/` remain independent of
  the platform. `cloud/api.py` authenticates, then `instrumentation/service.py`
  persists immutable execution/outcome assertions within the tenant scope.
- Tenant/workflow/version/run identify a run; execution and physical charge IDs
  identify deliveries and charge ownership. New SDK registry keys include the
  authenticated tenant. Shared `instrumentation/identity.py` joins exact public
  identities, or proven legacy mappings; bare run IDs and ambiguous mappings do
  not establish ownership. Concurrent replays use the same full assertion check.
- Costs default to unknown. Explicit caller amounts preserve their provenance;
  measured usage, rate-card estimates, reported charges and invoice allocation
  are separate. A charge has one retained owner; structural summaries never add
  dollars. Legacy collapsed attempts, lost digits and ambiguous zero stay uncertified.
- `instrumentation/charge_costs.py` resolves append-only charge revisions without
  changing the owner or attempt count. Replacements cover all cost components and
  provenance; zero refunds the asserted amount and unmeasured withdraws it. Erasure
  removes revisions before owners; retained report arithmetic remains available.
- Outcomes follow an immutable workflow-version definition. Latest matching typed
  assertions must be final to resolve success; provisional/withdrawn/invalid latest
  evidence cannot revive an older success. Technical completion or cancellation is
  separate from business acceptance. Cancelled framework calls retain interruption
  evidence and propagate cancellation unchanged.
- `econ/decisioning.py` preserves exact count ratios and Decimal summary totals.
  New `interval-policy/1` comparisons gate verdicts on Newcombe, Wilson and
  log-ratio cost intervals before display rounding; an unresolved interval abstains.
  Comparisons require an explicit quality floor. A pass recommends review and
  does not certify causal savings or independent source truth. Historical reports
  keep their recorded method and verdict.
  Hosted experiments price observed incumbent/candidate/judge usage separately;
  missing or unsupported usage remains unresolved.
  `econ/backtest_evidence.py` defines retained evaluator settings and paired numeric
  scores by original case index. The hosted engine projects existing `run_eval`
  results into that content-free object; replay and judge errors force abstention.
  The backtest adapter/service retain it in existing report JSON. Historical
  absence stays null, and exact retries do not run the provider again. The rubric
  hash and evaluator version bind submitted scoring rules; provider defaults,
  alias revisions and judge validity remain unverified. Debug score discrepancies
  from `evaluation_evidence.cases` and the caller's digest-bound ordered inputs.
  Live debugger and reconciliation services explicitly label their calculation
  revisions; schema defaults keep missing historical methods `legacy_unversioned`.
  JSON and Markdown preserve this label, but live-query source selection is not frozen.
- Caller-owned source inventories originate before delivery and close a fixed
  execution window. Missing/unexpected events, changed digests or overflow cause
  abstention; incomplete run costs make comparison CPO unavailable. The SDK is
  synchronous and caller-managed; buffered adapter capture is best-effort, not a
  durable outbox. Callers must retain payloads for retry after uncertain delivery.
- Retained decisions bind selected source/definition/inventory digests and normalized
  `calculation_inputs` with run multiplicities. These reconstruct arithmetic after
  late evidence/erasure, not independent source truth or an atomic raw snapshot.
  Changed policy/selected evidence yields a new revision; identical requests and
  evidence retain the original ID/time. Old reports keep their original semantics.
- Applications declare new versions for economics-relevant configuration changes.
  Same-version comparisons and old windows remain descriptive, with no automatic
  freshness cutoff. Debugger requests exceeding 50,000 execution events return 422
  rather than omit older events. Narrow the window on that response.
- Native execution acknowledgements expose server-owned UTC `ingested_at`, separate
  from source event time. Exact retry preserves first arrival; historic unknown
  arrivals stay null. Investigate a discrepancy from source IDs, acknowledgements,
  stored rows, revision history and the retained calculation/definition bindings.

See `packaging/sdk/README.md`, `docs/concepts/economic-optimization.md`,
`docs/how-to/economic-debugger.md`, `docs/how-to/provider-bill-reconciliation.md`
and `docs/backend-import-migration.md` for contracts and precise calculation rules.
Matching aggregate estimates are not request-level invoice facts. All provider
money stays visibly unallocated where measured request-dollar weights are absent.

## Deployment and rollback

Run `zeroth migrate-econ` before serving a PostgreSQL database. The standalone
cloud image applies the economic chain in `alembic_version_econ`; the platform's
other migration chain remains separate. Readiness returns 503 for a noncurrent
schema or a missing/finished enabled scheduler task. A live task does not prove
successful recent work. No deployment is implied by a local merge.

Economic head `20260908_26` joins the qualification registry (`20260904_21`)
with the combined forecast and execution-evidence history (`20260907_25`).
The merge revision performs no DDL; upgrading either parent applies its missing
branch before recording the shared head.
Evidence migrations preserve public identity, source windows, physical ownership,
outcome maturity, charge revisions, exact original costs and first arrival time.
Historical unknowns are not backfilled with invented meaning.

Stop application writes before an SQLite exact-cost upgrade. That migration uses
an offline connection with foreign_keys=OFF and preserves IDs, child rows and
indexes; run PRAGMA foreign_key_check before enabling enforcement again. SQLite
amounts are exact decimal text, PostgreSQL uses Numeric(18,8). Sum the scoped
Decimal reader values, not SQLite SQL arithmetic that coerces text to floats.
Historical mapper values are preserved; already-lost decimal digits cannot recover.

Prefer application rollback to a compatible reader that keeps additive columns,
revision tables and retained report formats. Evidence downgrades refuse to discard
populated protected fields/history. Forecast revision 20 downgrade destroys report
artifacts/delivery audit; revision 19 destroys schedules, assignments, verification
and calibration history. The merge revision itself has no destructive operation.
No assembled rollback image or release gate is certified by local integration.

Solo remains $39/month, monthly only, a 14-day trial, WorkOS AuthKit, Paddle and
Railway/Postgres, with no Team sale. Its 155 shared decision scans and up to five
schedules retain the existing minimum 24-hour interval; trial allowance is one
scan. Verified billing events own entitlement, not redirects. History reads do not
reserve scans. Debug quotas in `cloud/entitlements.py` and scheduler `last_error`.
Release/restore/vendor procedures remain in `docs/operations/hosted-commerce.md`
and the existing launch-policy/evidence runbooks. Independent provider truth,
customer adoption and full R2 compatibility are distinct acceptance obligations.

## Runtime and economic service ownership

- `integrations/execution/sandbox.py` and the MCP Docker transport retain ownership
  of started containers through cancellation. `platform/primitives/cancellation.py`
  finishes cleanup despite repeated cancellation and then propagates cancellation.
  The sandbox executor tracks the underlying worker future through shutdown;
  cancelling an asyncio waiter does not mean its synchronous worker stopped.
- `integrations/persistence/runs/run_repository.py` writes checkpoint state and
  thread references in one transaction. Every path, including standalone checkpoint
  writes, verifies the current lease worker, generation and expiry. A stale lease
  raises `FencedRunWriteRejectedError`; a live lease with changed run status raises
  `RunStatusConflictError`. Neither failure may leave a partial checkpoint.
- `runtime/agents/provider.py` distinguishes malformed structured output from
  provider transport errors so the runtime can apply its configured validation retry.
  Governed HTTP clients share a TLS context rather than loading roots on each call.
- `econ/plane/decisioning/lifecycle.py` owns one scheduler per service lifetime.
  Tenant failures are isolated, retained errors omit private SQL details, and
  readiness reports whether a required scheduler is missing or finished.
- The qualification registry binds immutable records to tenant, policy, algorithm,
  source window and evidence identity. Lookup is server-owned; public requests may
  reference a record but cannot issue one. Public predictive authorization remains
  closed. Deactivation and revocation preserve retained history.
- `econ/plane/statistics/intervals.py` contains sample-based interval estimators.
  Cost estimation separates measured totals from the inferred subset; counterfactual
  value estimates use declared class values, ordered drift checks and a minimum
  bootstrap sample. These calculations do not authorize customer rollout.
- SDK transport errors expose `original_error` through `ZerothTransportError` and
  remain catchable as `httpx.RequestError`. API failures have typed status-specific
  exceptions with bounded, sanitized details. Retrying uncertain delivery still
  requires the caller's original idempotent payload.

Debug cancellation with `tests/execution_units/test_runner_cancellation.py`,
checkpoint ownership with `tests/persistence/test_checkpoint_fence_contract.py`,
and scheduler ownership with `tests/econ_plane/test_scheduler_lifecycle_ownership.py`.
Release compatibility checks bind installed versions, source bytes and image IDs
before accepting test results. Historical evidence is restored through
`scripts/restore_release_inputs.py`; it is not regenerated by changing a version label.

## Current risks and unfinished work

- Production predictive validity and customer sufficiency remain unestablished.
  The evaluation contract and reference inputs are pinned by `release/inputs-v1.json`
  and restored from the external archive before evaluation.
  The evaluation CLI intentionally fails while
  required delivery, fidelity, mutation, or statistical gates are unresolved.
- Scenario pairing now uses IDs for joins and paired outcome content for canonical
  ordering, preserving independent duplicate-valued units. Common routing uniforms
  prevent action order changing draws. Bootstrap size equals independent paired
  count; runtime scales with that count (the previous 500-case cap is removed).
- CVaR integrates fractional tail mass; quantiles reject empty/nonfinite inputs and
  invalid probabilities. Numerical agreement alone does not establish calibration.
- Missing critical outcomes survive API/SDK JSON round trips as missing, not false.
  Daily telemetry demand remains unknown-horizon and abstains; no automatic month conversion.
  Calibration deduplicates identical forecast IDs per metric and rejects conflicting copies.
- Nested future-request sampling uses a 50m planned-work ceiling and fixed-N 99%
  numerical bounds. Overflow abstains before RNG construction; no
  demand/evidence truncation or runtime enable flag is provided. Algorithm version
  `nested-paired-monthly-v3-hoeffding99-math1-predictive1` enters new request digests; old records remain unchanged.
- Descriptive synthetic evaluation does not establish predictive validity.
  Generative-law adapters are bound to the frozen protocol and amendments supplied
  by the external release-input archive.
- Local SMTP acceptance followed by a crash currently loses the uncommitted audit
  attempt; partial recipient rejection is silently ignored. These are reproduced
  failing gates, not repaired behavior. No delivery-state schema change is approved.

- The renamed platform and SDK releases are locally verified but not published. PyPI still
  returns 404 for `zeroth-platform` and `zeroth-sdk`; `zeroth-core` remains at `0.1.0`.
- Public README positioning and availability wording will be reconciled after registry
  publication, rather than claiming an install path is live before it is verified.
- Managed provider backtests do not yet emit case-level cost/latency artifacts by default; only
  explicit artifacts are harvestable, and aggregate records abstain.
  Empty or missing retained observation/demand arrays use the existing
  `no_paired_case_level_evidence` path: API refresh retains an abstention and scheduled
  refresh records its decision ID without `last_error`. Nonempty malformed artifacts
  still fail validation. Regression entry point:
  `tests/econ_plane/test_empty_backtest_evidence.py`. This repair changes only the
  harvester guard; no schema migration, measurement synthesis, or cutoff change is required.
- Calibration assessment is intentionally small and transparent. Explicit SMTP report delivery is
  implemented; automatic scheduled delivery and calibration-drift notification rules are not.
- PDFs are stored in the relational database for the POC. Move large or high-volume artifacts to a
  tenant-scoped object store before scaling report volume materially.
- The simulator forecasts cost, quality, latency, and critical errors. Separate token-demand,
  capacity, retry-policy, and delivery-time models are not implemented.
- Randomized verification cannot identify interference, unrecorded treatment changes, or missing-not-
  at-random outcomes. These invalidate the study operationally even when the software sees no flag.
- Willingness to pay for this closed loop remains unproven; technical completion is not market proof.

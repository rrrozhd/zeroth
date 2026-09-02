# Zeroth Project Model

## Purpose and boundary

Zeroth is a governed execution and economic decision platform for production AI workflows. Its
runtime orchestrates graphs, agents, tools, memory, approvals, and executable units under explicit
policy and audit boundaries. Its economic layer captures versioned execution, cost, latency,
outcome, and provenance evidence, then turns that evidence into inspectable change recommendations.

The first probabilistic decision concerns moving a workload from an incumbent model
to a candidate. Currently every public forecast remains experimental and abstains;
its action distributions are diagnostics, not permission to ship or reroute.
Zeroth does not modify customer routing.

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
- `zeroth-sdk` is the lightweight remote Python client. Its first public version is `0.1.0` and
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
4. Each scenario resamples complete paired runs, samples observed demand, applies a global share or
   explicit cohort route, and aggregates cost, quality, p95 latency, and critical errors.
5. The private diagnostic tests each action against point-estimated chance constraints and
   empirical CVaR. The public wrapper retains diagnostics but returns abstain/collect_evidence,
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
- Release candidate verification on 2026-09-02: 12,623 passed, 9 skipped, and 465
  deselected by the repository's default test selection. Semantic lint, changed-file
  formatting, docs-reference scanning, all four distribution builds, and clean installs passed.
- Debug a retained report from `probabilistic_migration_decisions.report_json`, then compare its
  `request_digest`, evidence snapshot, policy, seed, and calibration state.

## Deployment and rollback

Apply `uv run alembic -c alembic-econ.ini upgrade head` before serving the new routes. The service
remains advisory, so rollback does not require reverting customer traffic. Application rollback
may leave the additive tables in place. Downgrading revision 20 destroys generated report artifacts
and delivery audit rows. Downgrading revision 19 destroys schedules, assignments, verification
reports, and calibration history, so export the applicable records before rollback.

## Current risks and unfinished work

- Frozen economic evaluation contract v1 is now under independent acceptance testing.
  Earlier test counts above describe the source baseline, not acceptance under this
  contract. Production predictive validity and customer sufficiency remain blocked.
  See `docs/operations/economic-evaluation-implementation-status.md` for current
  evidence and `docs/operations/economic-evaluation-design-decisions.md` for pending
  public-contract decisions. The new evaluation CLI intentionally fails while
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
- The current private model resamples historical units then scales by demand. It omits actual
  future-request variability and can produce fractional-request losses. The exact support
  gate remains red. A nested future-request model and 50m work ceiling await owner approval.
- The v3 descriptive synthetic protocol and its summary-stream supplement are independently
  approved. The harness must be committed and identity-checked before its initial run.
  The required 930 forecast summaries do not imply predictive acceptance.
- Local SMTP acceptance followed by a crash currently loses the uncommitted audit
  attempt; partial recipient rejection is silently ignored. These are reproduced
  failing gates, not repaired behavior. No delivery-state schema change is approved.

- The renamed platform and SDK releases are locally verified but not published. PyPI still
  returns 404 for `zeroth-platform` and `zeroth-sdk`; `zeroth-core` remains at `0.1.0`.
- Public README positioning and availability wording will be reconciled after registry
  publication, rather than claiming an install path is live before it is verified.
- Managed provider backtests do not yet emit case-level cost/latency artifacts by default; only
  explicit artifacts are harvestable, and aggregate records abstain.
- Calibration assessment is intentionally small and transparent. Explicit SMTP report delivery is
  implemented; automatic scheduled delivery and calibration-drift notification rules are not.
- PDFs are stored in the relational database for the POC. Move large or high-volume artifacts to a
  tenant-scoped object store before scaling report volume materially.
- The simulator forecasts cost, quality, latency, and critical errors. Separate token-demand,
  capacity, retry-policy, and delivery-time models are not implemented.
- Randomized verification cannot identify interference, unrecorded treatment changes, or missing-not-
  at-random outcomes. These invalidate the study operationally even when the software sees no flag.
- Willingness to pay for this closed loop remains unproven; technical completion is not market proof.

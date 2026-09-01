# Goal

Turn Zeroth's economic debugger into a defensible, low-touch adoption wedge for
the future paid organization FinOps/governance service.

# Acceptance evidence

- One authenticated report endpoint and headless CLI produce an evidence-bounded
  diagnostic from the working instrumentation-to-plane contract.
- A seeded execution/outcome fixture renders into a downloadable Markdown artifact.
- Public docs state the free/paid boundary, activation funnel, privacy rules,
  deployment prerequisites, conversion metrics, and stop criteria.
- An asynchronous pilot issue form collects aggregate demand without traces,
  prompts, secrets, or subject identifiers.
- Route/schema/package contracts and impacted tests pass; changes are committed
  locally on `main` and are not pushed.
- Immutable, tenant-scoped outcome definitions bind a workflow version to one
  terminal outcome type and success predicate; undefined outcomes cannot produce
  cost-per-success claims.
- An immutable provider statement import and headless reconciliation command
  allocate billed dollars through measured workflow/outcome evidence while
  preserving unmatched buckets, telemetry variance, and unresolved outcomes.

# Milestone status

- Existing reconciliation/backtest/export/SDK boundaries audited: complete.
- Diagnostic endpoint and CLI implementation: complete; focused tests green.
- Asynchronous organization activation signal: complete; issue form validated.
- Monetization and operator documentation: complete; strict docs build green.
- Contract, packaging, impacted-suite, and full-suite verification: complete.
- Explicit outcome-semantics slice: complete; migration, API, CLI, contracts,
  documentation, and repository-wide tests verified.
- Provider-bill paid wedge: implementation, impacted verification, wheel build,
  strict docs build, and repository-wide verification complete.
- Public buyer intake: narrowed to the single provider-bill closure hypothesis;
  spend/process/export/problem/budget signals and public-data limits are tested.
- OpenAI activation path: one complete Costs API JSON page can be normalized
  offline without giving Zeroth provider credentials; incomplete or unsupported
  financial inputs fail before a statement is written.
- Public first screen: leads with provider-bill closure and its two-command
  activation path; generic debugging, backtests, and preserved runtime surfaces
  are explicitly secondary.
- Distribution-gate repair: complete locally; the pinned Linux/ARM load profile,
  release suites, fresh core wheel, headless CLI, and UI exclusion are verified.
- Economic-debugger promotion contract: implemented locally; exact TestPyPI
  core bytes, headless exclusions, bounded diagnostic, and provider closure now
  gate promotion. Legacy platform acceptance remains manual and non-promoting.
- Self-serve value demonstration: implemented locally; one installed-wheel
  command produces a synthetic, claim-bounded diagnostic and closed provider
  reconciliation pack without a server, JWT, UI, or provider credential.
- Real-data client activation: implemented locally; the shipped instrumentation
  client authenticates with a static bearer or environment token and confirms
  both execution and outcome persistence before an onboarding check succeeds.
- Low-touch demand experiment: specified; one conditional Show HN launch uses
  an installable demo, one public issue CTA, explicit artifact/source fields,
  a 14-day window, and commercial rather than attention metrics.

# Decisions

- The report is a free trust artifact, not the paid SKU.
- Failed-run cost is exposure, not step causality; savings are never inferred.
- The first paid boundary is actual provider-invoice reconciliation plus
  organization rollups/governance, not the existing calibration summary.
- The UI and release-blocked standalone SDK remain unchanged.
- Outcome events remain unchanged. An immutable definition keyed by workflow and
  workflow version selects the outcome type and predicate, so caller-authored
  labels cannot silently rewrite historical economics. Changing the rule requires
  a new workflow version.
- Official provider cost surfaces aggregate by time and provider dimensions, so
  the first paid contract imports normalized buckets and discloses proportional
  measured-cost allocation. It does not pretend provider request IDs form a
  portable invoice join.

# Risks

- A useful report may not generate organization-shaped buying intent.
- No repository evidence proves a production hosted service, tenant isolation,
  backups, terms, or billing readiness.
- Public pilot intake must not collect sensitive runtime evidence.
- Concurrent first-write requests for the same outcome definition still rely on
  the database uniqueness constraint; a losing request can surface a conflict
  instead of the ordinary idempotent replay response.
- No real customer export has passed through the normalized provider-bill API;
  format usefulness and willingness to pay remain unvalidated.
- Provider credentials, automatic adapters, negative credits/tax adjustments,
  durable pre-aggregation, and signed evidence remain outside this slice.
- The new candidate/promotion workflows have not run on GitHub. TestPyPI must
  trust `release-zeroth-core.yml`; PyPI must trust
  `promote-zeroth-core.yml`; and the `pypi` environment should require a human
  reviewer. Those settings are external and are not proven by repository tests.
- The synthetic demo lowers comprehension friction but cannot validate
  instrumentation fit, real provider exports, willingness to pay, or the hosted
  organization product. Its executions must not inflate pilot funnel metrics.
- Static client tokens expire and are suitable only for short-lived setup or
  pilot scripts. Long-running services still need `headers_provider` token
  rotation, and a hosted credential issuer remains unimplemented.
- Show HN currently requires an established, community-familiar account and
  active asynchronous participation. Account eligibility is external state; do
  not evade it or substitute untracked cross-posting.

# Evidence

- Focused report/CLI and debugger tests: 18 passed before the transport remediation.
- Economic and plane suites: 345 passed after the remediation.
- Architecture, contracts, security, and wheel tests: 1,403 passed after the
  service-layer CLI move.
- Wheel contains `zeroth/service/economic_diagnostic_cli.py` and maps the
  `zeroth-econ` entry point to that module.
- Prior economic-plane verification and 10,000-run reconciliation are recorded
  in `PROJECT_MODEL.md` and the positioning evaluation.
- The first full-suite run found one introduced ungoverned `httpx.Client` site.
  Replacing it with the governed client exposed a forbidden econ→integrations
  dependency; moving network composition to the service domain resolved both
  ratchets without adding exceptions.
- Fresh full suite after remediation: 12,260 passed, 9 skipped, 465 deselected;
  651 pre-existing warning emissions, zero failures, in 867.41 seconds.
- Outcome-definition verification: 350 economic/plane tests, 1,368
  architecture/contract/security tests, 43 schema/acceptance tests, strict docs,
  Ruff, and wheel build passed.
- Fresh repository-wide verification after the README first-screen correction:
  12,272 passed, 9 skipped, 465 deselected, 652 warnings, zero failures, in
  888.63 seconds.
- Provider-bill verification: 363 economic/plane tests; 2,620 architecture,
  contract, security, and cross-tenant tests (3 skipped); 56 acceptance tests;
  82 documentation/public-surface tests (1 skipped); strict MkDocs; Ruff; wheel
  build; and 18 wheel-boundary tests passed.
- Fresh repository-wide verification after provider-bill reconciliation:
  12,301 passed, 9 skipped, 465 deselected, 651 warnings, zero failures, in
  878.53 seconds.
- Narrowed buyer-intake verification: 2 focused contract tests; 84 documentation
  and public-surface tests (1 skipped); Ruff; and strict MkDocs passed.
- Offline OpenAI Costs activation verification: 1,076 CLI, provider-bill, and
  library-surface tests; the complete 82-test economic suite; 84
  documentation/public-surface tests (1 skipped); strict MkDocs; Ruff; wheel
  build; 18 wheel-boundary tests; and live CLI help smoke tests passed.
- First-screen conversion verification: 3 focused positioning/legacy-safety
  tests and 85 documentation/public-surface tests (1 skipped), Ruff, and strict
  MkDocs passed. The preserved capability matrix, gateway warning, and
  `## Quickstart` remain inside the first 5,000 README characters.
- Fresh pinned Linux/ARM load profile after harness-pressure remediation: 1
  passed in 426.88 seconds; 2,348 observations; zero evidence errors; overload
  throughput 29.999/s, p99 486.95 ms, queue depth 9, recovery 1.20 s, and zero
  lost or duplicate accepted runs.
- Load and release-gate suites: 497 passed, 1 skipped. The exact CI release
  subset passed 79 tests with 2 infrastructure-dependent skips after restoring
  the host's full lockfile environment.
- Fresh `zeroth-core` 0.25.7.3 sdist and wheel built outside `dist/`; a clean
  wheel-only environment imported economic instrumentation, started both CLIs,
  and proved `zeroth-console` was not installed.
- Fresh wheel-only economic acceptance passed end to end against an isolated
  SQLite plane: one success, one failed run with a paid retry, `$0.50000000`
  measured/provider closure, `economic_risk_observed`, no UI or SDK, and a
  candidate-bound gate verdict of `passed`.
- Promotion is now two-stage and non-circular: the release run stops at an
  attested candidate bundle; a separate manual workflow binds run ID, exact
  digest, confirmation, actor, and `pypi` environment approval before final
  validation and publication.
- Final release/load-contract verification: 530 tests passed with 1
  environment-dependent skip; Ruff and strict MkDocs passed. A fresh
  0.25.7.3 wheel replay again produced `economic_risk_observed`, complete
  `$0.50000000` reconciliation, headless exclusions, and a passing gate.
- External release audit on 2026-08-31: GitHub environments `pypi` and
  `testpypi` exist with zero protection rules; repository Actions secrets and
  variables both report zero entries. The latest public release is `v0.4.1`,
  PyPI serves only `zeroth-core==0.1.0`, and `zeroth-sdk` is absent. PyPI and
  TestPyPI trusted-publisher registrations are not exposed by these repository
  APIs and remain unverified.
- Self-serve activation verification: 8 focused demo and public-funnel tests
  passed. A clean `zeroth-core[regulus]` wheel environment generated five
  artifacts, observed `$0.40000000` failed-run exposure and a `$0.10000000`
  repeated attempt, reconciled the full `$0.50000000` synthetic provider bill,
  and confirmed the standalone SDK remained absent.
- Impacted economic, plane, documentation, architecture, and release-contract
  verification: 2,139 passed with 1 environment-dependent skip; strict MkDocs
  and Ruff passed.
- Authenticated activation verification: 1,160 focused public-surface,
  instrumentation, architecture, and release tests passed before the final
  impacted run. A fresh wheel then started the standalone plane, persisted
  three executions and two outcomes through the installed authenticated
  client, produced `economic_risk_observed`, and closed the full
  `$0.50000000` provider statement.
- Final impacted economic, plane, documentation, architecture, and release-gate
  verification: 2,534 passed with 1 environment-dependent skip; strict MkDocs
  and Ruff passed.
- Launch-experiment verification: 58 documentation, public-surface, and legacy
  boundary tests passed with 1 environment-dependent skip; strict MkDocs and
  Ruff passed. The issue form requires both artifact stage and discovery source.

# Remediation bound

Fix failures introduced by this slice and deterministic contract drift. Do not
expand into hosted infrastructure, billing, SSO, or provider integrations here.

# Next action

Verify and commit the self-serve activation slice locally. Do not push or
publish without owner direction. The external activation path is then:
confirm both workflow-specific trusted publishers, push and run the candidate
gates, dispatch digest-bound promotion, exercise one real Costs API response
through the normalized import, and distribute the asynchronous closure test to
qualified production AI teams. Do not build a broader connector or
organization shell before those commercial evidence gates.

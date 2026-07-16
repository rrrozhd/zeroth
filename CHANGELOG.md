# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.1.12] - 2026-07-16

### Fixed

- **Condition evaluator refuses underscore-prefixed attribute access on
  non-Mapping objects** (audit S5 hardening). `payload.user.__class__`-style
  expressions on a real Python object (str, list, etc.) would open the
  introspection/gadget path; the `ast.Attribute` case now raises
  `ConditionEvaluationError` for any `_`-prefixed attr on a non-Mapping value.
  Dict/JSON key access is unaffected — it goes through the Mapping branch.

## [0.10.1.11] - 2026-07-16

### Fixed

- **Cancelled-then-replayed runs resume from where they stopped** (self-re-audit
  of the F3 fix). `_external_stop` returned the freshly-read run row (whose
  `pending_node_ids` is the stale pre-dispatch `[]`) instead of persisting the
  in-memory run, which already holds this hop's history and the successors queued
  for the next hop. A `FAILED->PENDING` replay (or interrupt resume) therefore
  started from an empty queue and was marked COMPLETED with the remaining nodes
  silently skipped. It now persists the in-memory run under the operator's status
  (save_run leaves lease columns untouched, so the cleared lease is preserved).
  Added a cancel->replay resume test.

## [0.10.1.10] - 2026-07-16

### Security

- **Erasure now clears `failure_state` (and `condition_results`/`channels`)**
  (self-re-audit of the F1 fix). `redact_run_in_transaction` nulled `error` but
  not `failure_state` — and `error` re-derives FROM `failure_state.message` on
  read (`_fill_governed_defaults`), so for FAILED runs the plaintext resurfaced
  and right-to-erasure was still a false claim. The redact UPDATE now also nulls
  `failure_state` and resets the free-form `condition_results`/`channels` dicts.
  The retention PII scan + seed cover these columns, plus an assertion that a
  reloaded erased run keeps `error is None`.

## [0.10.1.9] - 2026-07-16

### Security

- **Closed the webhook dead-letter cross-tenant IDOR left open by v0.10.1.3**
  (self-re-audit of the F8 fix). `require_deployment_scope` gates *who* calls a
  route, not *which* rows return, and the `webhook_dead_letters` table has no
  tenant column — so `GET /webhooks/dead-letters` and
  `POST /webhooks/dead-letters/{id}/replay` still leaked/replayed other tenants'
  dead-letters. Both are now scoped to the served deployment's own subscription
  set (list filters, replay 404s a foreign dead-letter before re-enqueuing). Also
  tightened the `get`/`deactivate` subscription guard to compare `deployment_ref`
  (not just `tenant_id`), matching the list route, so a same-tenant admin of one
  deployment can't read/deactivate another deployment's subscription. Added
  cross-tenant dead-letter + cross-deployment regression tests.

## [0.10.1.8] - 2026-07-16

### Fixed

- **Cooperative cancellation now covers parallel/subgraph fan-out** (audit F3
  follow-up, completing v0.10.1.6). The four fan-out end-of-hop RUNNING writes
  (parallel fan-in, resumed fan-in, synchronous subgraph, resumed subgraph) now
  call `_external_stop` before persisting, so an operator cancel/interrupt that
  lands while a fan-out node's branches run is observed at the fan-in instead of
  being clobbered. Added a parallel fan-in cancel test (branch cancels mid-fan-out
  -> the node after the fan-in never dispatches); all 309 execution-path tests
  still pass.

## [0.10.1.7] - 2026-07-16

### Security

- **Scoped `get_deployment_cost` to the served deployment** (audit F4 follow-up).
  `GET /v1/deployments/{ref}/cost` took the deployment ref from the path and
  proxied it to Regulus unscoped, so any admin could read an arbitrary
  deployment's spend by ref. It now 404s unless the ref is this service's own
  served deployment and the caller owns it (`require_deployment_scope`). Added
  foreign-ref and cross-tenant 404 regression tests.

## [0.10.1.6] - 2026-07-16

### Fixed

- **Operator cancel/interrupt now actually stop an in-flight run** (audit F3,
  verified P0). The orchestrator drive loop blind-wrote `RUNNING` on every node
  hop, clobbering an admin `POST /admin/runs/{id}/cancel` (→ FAILED) or
  `/interrupt` (→ WAITING_INTERRUPT) written mid-dispatch, so the run ignored the
  operator and drove to completion. The loop now re-reads the persisted status
  (`_external_stop`) at the top of each iteration and again before its end-of-hop
  RUNNING write, adopting and returning the operator's terminal/paused state
  instead of overwriting it. Added driving tests that flip the status mid-dispatch
  and assert the next node never runs.
  - Scope note: fully covers the common sequential-agent path (the confirmed
    defect). Parallel/subgraph fan-out branches keep their own RUNNING writes;
    the top-of-loop check gives them best-effort (between-hop) cancellation, and
    guarding each fan-out write is a follow-up.

## [0.10.1.5] - 2026-07-16

### Security

- **Fixed right-to-erasure leaving plaintext and crashing on encrypted
  deployments** (audit F1, verified P0). (a) `redact_run_in_transaction` never
  cleared `runs.execution_history`, so every node's plaintext prompt/response
  survived an erasure the API reported as complete — it is now reset to `[]`.
  (b) `erasure_payloads_in_transaction` read `run_checkpoints.state_json` without
  decrypting, so on any at-rest-encrypted deployment erasure (and the TTL purge
  worker) raised `JSONDecodeError` and rolled back to a no-op — it now decrypts
  first, mirroring `get_checkpoint`. Added regression coverage: the full-surface
  erasure test now seeds and scans `execution_history`, plus a new
  erase-on-encrypted-deployment test.

## [0.10.1.4] - 2026-07-16

### Security

- **Added the missing RBAC gate to the econ_plane costing router** (audit F7,
  verified — downgraded from the audit's P0 "anonymous" claim). Unlike every
  sibling econ router, `costing/api.py` carried no auth dependency, so the
  pricing catalog + cost profiles that back every cost estimate were writable by
  any caller that reached the router (anonymous in a standalone deploy; any
  authenticated principal through the mount). Writes now require the `Admin` econ
  role, reads require any valid role. Added a mount regression test. The mounted
  topology was never anonymously writable (the `/regulus` Zeroth API-key gate),
  and the console econ dashboards (read-only proxy) were unaffected.

## [0.10.1.3] - 2026-07-16

### Security

- **Fixed a cross-tenant IDOR in the webhook API** (audit F8, verified P0).
  `create_subscription` trusted the request body's `deployment_ref`/`tenant_id`,
  letting any tenant admin subscribe to another tenant's run/approval/lifecycle
  events. Subscriptions are now bound to the served deployment + tenant (body
  values ignored), and every webhook route (create/list/get/deactivate +
  dead-letter list/replay) requires the caller to own the served deployment via
  `require_deployment_scope`; list is forced to the served tenant and
  get/deactivate reject a foreign subscription_id as 404. Added tenant-isolation
  regression tests.

## [0.10.1.2] - 2026-07-16

### Security

- **Fixed a cross-tenant IDOR in the tenant cost/budget API** (audit F4, verified
  P0). `GET /v1/tenants/{id}/cost` and `PUT /v1/tenants/{id}/budget` took the
  tenant id from the URL path and never checked it against the caller's tenant,
  so any tenant admin could read another tenant's month-to-date spend and
  overwrite (zero-out / lift) their budget cap. Both handlers now call
  `require_resource_scope` before doing any work. Added cross-tenant 404
  regression tests and corrected the existing tests that had encoded the bug by
  asserting cross-tenant success. `get_deployment_cost` is unscoped the same way
  and is flagged as a follow-up (lesser: single served-deployment aggregate).

## [0.10.1.1] - 2026-07-15

### Added

- Tests for the econ dashboard proxy (F7): asserts the `/v1/econ/dashboard/*`
  routes are registered, require auth (401), enforce `METRICS_READ`, and reach the
  handler's Regulus guard (503, not 404) — plus the no-`/v1` compat alias.

## [0.10.1] - 2026-07-15

### Added

- **Econ portfolio dashboard, console-reachable** (F7). New core service module
  `econ_dashboard_api` proxies the bundled Regulus dashboard suite (kpis,
  top-creators, capital-destroyers, capability-ranking, and the trend/quality
  views) under `/v1/econ/dashboard/*`, behind `METRICS_READ`, using the same
  server-side self-auth bridge as `cost_api`. The console previously could not
  reach these at all (the `/regulus` JWT issuer is gated off). The Cost page gains
  a Portfolio economics card (spend/value/net-margin/confidence/efficiency plus
  top value creators and capital destroyers), and the OpenAPI spec was
  regenerated to include the new routes.

## [0.10.0.7] - 2026-07-15

### Added

- **Console: Prompt templates page** (F9), previously API-only. New `/templates`
  page (+ nav entry) to list, register (with version + variables), preview, and
  delete prompt templates. Wires `GET/POST /v1/templates` and
  `DELETE /v1/templates/{name}/{version}`.

## [0.10.0.6] - 2026-07-15

### Added

- **Console: Integrations (webhooks) page** (F8), previously API-only. New
  `/webhooks` page (+ nav entry) to create webhook subscriptions (signing secret
  shown once on create), list and deactivate them, and view/replay dead-lettered
  deliveries. Wires `GET/POST/DELETE /v1/webhooks/subscriptions` and
  `GET /v1/webhooks/dead-letters`, `POST /v1/webhooks/dead-letters/{id}/replay`.

## [0.10.0.5] - 2026-07-15

### Added

- **Console: Retention & Compliance page** (F1) — the flagship v0.9 GDPR / EU-AI-Act
  surface, previously API-only. New `/retention` page (and nav entry) to view/edit
  the tenant retention policy (purge toggle + run/audit TTLs), place and release
  legal holds, and run right-to-erasure with a confirm step and the 409-on-hold
  path surfaced. Wires `GET/PUT /v1/retention/policy`,
  `POST/DELETE /v1/retention/legal-holds`, `POST /v1/retention/erasure-requests`.

## [0.10.0.4] - 2026-07-15

### Added

- **Console: compliance evidence + deployment-wide chain verification** (F2). The
  Audit page now shows a deployment-wide tamper-evidence badge
  (`GET /v1/deployments/{ref}/audit-verification`, three-state) and exports the
  full compliance evidence bundle (`GET /v1/deployments/{ref}/evidence`); each
  audit row exports its run's bundle (`GET /v1/runs/{id}/evidence`).

## [0.10.0.3] - 2026-07-15

### Added

- **Console: tenant budget card** (F4). The Cost page now shows a tenant's
  month-to-date spend vs its enforced budget cap and lets an operator set the cap
  (`GET /v1/tenants/{id}/cost`, `PUT /v1/tenants/{id}/budget`), with an over-cap
  warning. Previously the console only surfaced deployment-scoped cost.

## [0.10.0.2] - 2026-07-15

### Added

- **Console: run operator controls** (F3). The run-detail view gains RUN_ADMIN
  cancel / interrupt / replay buttons (shown per run state), wiring
  `POST /v1/admin/runs/{id}/cancel|interrupt|replay`.
- **Console: quality-verdict attachment** (F5). Terminal runs now show a good/bad
  verdict control (`POST /v1/econ/quality-verdict`) — the signal that activates
  the previously-dormant quality-aware unit economics card on the Cost page.
- **Console: contract-driven run submission** (F11). The submit form fetches the
  deployment's pinned input contract, shows its JSON Schema, offers "Prefill from
  contract", and warns when the payload is missing contract-required fields.

## [0.10.0.1.1] - 2026-07-15

### Removed

- Deleted the dead econ_plane `statistics` router (`statistics/api.py`), an empty
  `APIRouter` with zero routes that `main.py` still mounted (F14). The statistics
  *package* stays — its `schemas` and `service` are used by reconciliation,
  costing, and counterfactual. No route-table or behavior change. (F13's blocked
  `/auth/token` issuer is left in place: it is already neutralized by the
  `app.py` gate and its handler is exercised by econ_plane's own tests.)

## [0.10.0.1] - 2026-07-15

### Added

- **Console: deployment attestation panel** (F12). The Overview page now surfaces
  the signed deploy-time attestation (graph/contract/settings digests, signing
  key) with a three-state self-verify badge, wiring the previously-orphaned
  `getDeploymentAttestation` / `verifyDeploymentAttestation` client functions.
- **Console: deployment rollback** (F6). The Deployments card exposes a
  DEPLOYMENT_ADMIN-gated "Roll back…" control that pins a new deployment version
  to an earlier graph version via `POST /v1/deployments/{ref}/rollback`.

## [0.10.0.0.4] - 2026-07-15

### Fixed

- Regenerated the console's committed OpenAPI artifacts (`frontend/openapi.json`
  and `frontend/app/lib/api-types.ts`), which were stale at v0.7 (50 paths) and
  internally inconsistent — `api-types.ts` had been hand-patched with attestation
  signature fields the spec never got. The fresh dump is 62 paths / 98 schemas
  and now includes the econ, retention, and attestation-verify routes. Closes the
  spec-drift findings (F15/F16) from the integration audit. Console app code
  typechecks clean against the regenerated types.

## [0.10.0.0.3] - 2026-07-15

### Added

- Regression tests for `InterruptManager` (`tests/runtime/test_interrupts.py`)
  covering the `resolve()` expiry path that raises `InterruptExpiredError`, so
  the v0.10.0.0.2 dangling-import fix cannot silently re-break. The manager had
  zero prior coverage.

### Changed

- `governed/PROVENANCE.md` notes `InterruptExpiredError` as the one symbol
  reconstructed locally (rather than moved) during the `governai` absorption,
  keeping the "move, not a rewrite" claim accurate.

## [0.10.0.0.2] - 2026-07-14

### Fixed

- Restored the `InterruptExpiredError` exception in
  `zeroth.core.governed.runtime.interrupts`, which the v0.10 `governai`
  absorption left dangling as an import from the never-vendored
  `governed.workflows` package. `InterruptManager.resolve()` now raises a
  locally-defined exception instead of `ModuleNotFoundError`.

## [0.10.0.0.1] - 2026-07-13

### Changed

- Docstrings and comments across the memory, agent-runtime, contracts,
  execution-units, and runs modules reworded from "GovernAI" to "governed" now
  that the framework slice is vendored in-tree (`zeroth.core.governed`). No
  behavior change. The public `GovernAIRedisRuntimeStores` /
  `build_governai_redis_runtime` identifiers and the `governai_kind` contract
  keys are retained for compatibility.

## [0.10] - 2026-07-13

### Changed

- **Absorbed the `governai` dependency in-tree.** Zeroth used only a curated slice
  of the external `governai==0.2.3` framework (memory types + connector wrappers,
  `RunState`/`RunStatus`, tool primitives, tool-call helpers, flow/step spec types,
  audit emitters); that slice is now vendored under `zeroth.core.governed` and the
  external dependency is dropped. Behavior is unchanged — a pure move, proven by the
  full suite and the `ScopedMemoryConnector` `SHARED → "__shared__"` isolation
  invariant. governai's execution kernel (`runtime/local`, `workflows`, `approvals`,
  `policies`, `agents`, `sandbox`) was intentionally left behind; zeroth runs its own
  orchestrator. Transitive deps `lark`, `langgraph-sdk`, and `ormsgpack` are shed.

### Removed

- `GovernedLLMProviderAdapter` (unused; the production LLM path is
  `LiteLLMProviderAdapter`), along with the `integrations.llm.GovernedLLM` binding.

## [0.9.1.2] - 2026-07-13

### Fixed

- **Studio saves no longer wipe node governance fields** — the canvas
  round-trip dropped `capability_bindings`, `policy_bindings`,
  `execution_config`, `audit_config`, and `parallel_config` on every
  structural save, silently stripping API-authored capability restrictions
  (security-relevant under v0.9's enforce-by-default). The Studio API now
  emits these fields in node `data` and, on save, preserves the stored value
  for any key the payload omits (an explicit value, including `[]`, still
  overrides). `node_version` and non-title display metadata also carry over
  instead of resetting.

## [0.9.1.1] - 2026-07-13

CI-gate hotfix for the 0.9.1 release candidate; no behavior changes.

### Fixed

- Docstring coverage restored above the CI `interrogate` gate (the v0.9/v0.9.1
  hardening code shipped under-documented helpers and API models).
- `docs/reference/configuration.md` regenerated to match current settings
  (regulus default-enabled, `FAIL_CLOSED`, `PER_RUN_CAP_USD`, secrets,
  provenance, and retention sections).

## [0.9.1] - 2026-07-13

v0.9 hardening pass — closes the findings of the 2026-07-12 audit across six
workstreams. Bug-fix release: no new product surface.

### Fixed

- **Runtime isolation** — concurrent runs no longer share mutable agent-runner
  state: each dispatch gets a dispatch-local runner (config, provider, memory
  resolver, context tracker, budget enforcer, tool executor), eliminating
  prompt/template crossover and wrong-tenant cost attribution under load.
- **Tenant-safe deployments** — deployment create/rollback/list are scoped to
  the authenticated principal's tenant; graphs carry workspace ownership, and
  Studio-authored deployments can no longer fall back to the `default` tenant.
- **Database coordination** — audit-chain appends serialize through database
  coordination rows (Postgres advisory locks / SQLite reserved rows), so
  multi-worker deployments cannot fork the audit hash chain; durable audit
  sequence numbers backfill in one statement; mixed/legacy chains recover.
- **Retention correctness** — `run_ttl_seconds` is enforced (previously
  persisted but ignored); TTLs validate as positive; audit TTL no longer
  erases whole runs with newer records; legal holds and erasure serialize
  race-free per tenant; erasure cleanup state is materialized instead of
  replaying the full retention log per operation.
- **MCP hardening** — declarative `mcp_servers` config now reaches generated
  runners; agents must hold `process_spawn` + `external_api_call` BEFORE an
  MCP server subprocess is spawned (enforced at dispatch, reported at publish
  validation as `missing_mcp_capability`).
- **Vault hardening** — secret resolution is async, pooled (one shared
  `httpx.AsyncClient`), and single-flight per key and per AppRole login; LLM
  key resolution, signing bootstrap, HTTP auth headers, and execution-unit
  secret injection all resolve off the event loop; the provider closes once
  at app shutdown.
- Repo-root test residue (`econ_plane.db`, `.zeroth/`) is isolated to session
  temp directories, removing an order-dependent test flake.

### Changed

- README, SECURITY.md, and `.planning/PROJECT.md` updated so product claims
  match implemented behavior (deployment restart caveat, budget enforcement
  requires the `regulus` extra, bare-install fail-open, current Next.js and
  embedded-econ-plane architecture).

## Pre-0.9 development series (2026-07-05 – 2026-07-12)

Versions `0.3` – `0.9` shipped in rapid succession from a private working
branch; their scope is summarized here from git history rather than
reconstructed as full entries.

## [0.9] - 2026-07-12

- Governed+secure parity: tenant isolation, capability enforcement, signed
  provenance, retention/right-to-erasure, pluggable secret provider (Vault).

## [0.8] - 2026-07-10

- Economic viability suite (cost-per-successful-outcome, waste rollups,
  model right-sizing) and the Regulus control plane absorbed into the
  `zeroth` namespace (`zeroth.econ_plane`, mounted at `/regulus`).

## [0.7] - 2026-07-08

- Agent tool edges (attached units as callable tools), message-list input,
  persistent conversations, per-node economics on the cost page, and the
  vendor-dd full-surface reference app.

## [0.6] - 2026-07-08

- Code node (author Python on the canvas, sandbox-executed), entrypoint
  node, and user-defined JSON-Schema contracts in the console.

## [0.5] - 2026-07-07

- Runtime connector management, publish/deploy from the API and console
  (the canvas→run loop closes), hardened Docker sandbox defaults.

## [0.4] - 2026-07-06

- Econ-plane auth holes closed, Studio/cost RBAC, console onboarding
  (guide page, workflow templates, inline help), console packaged as the
  `[console]` extra, full-screen Studio canvas, run-from-canvas replay.

## [0.3] - 2026-07-05

- Regulus economic control plane bundled in-repo; console UX quick wins;
  first public release lineage of the `0.x` series.


## [0.2.0] - 2026-04-13

v4.0 Platform Extensions — production-grade agentic workflow capabilities.

### Added

- **Subgraph composition** — nest published graphs as SubgraphNodes with governance inheritance, thread participation modes, approval propagation, and cycle detection (90 tests).
- **Parallel fan-out/fan-in** — split execution across branches with budget isolation, configurable merge strategies, and SubgraphNode-in-parallel guard (55 tests).
- **Template registry** — CRUD REST API for prompt templates with audit-safe secret redaction, `TemplateRegistry.delete()` method (59 tests).
- **Context window management** — token tracking with pluggable compaction strategies and thread persistence (68 tests).
- **Resilient HTTP client** — retry with exponential backoff, circuit breaker, rate limiting, and connection pooling (62 tests).
- **Artifact store** — content-addressed storage with TTL refresh, GET REST endpoint (auth-protected).
- v4.0 concept documentation pages for all six new subsystems.
- OpenAPI spec synced with v4.0 endpoints (artifact GET, template CRUD).
- Phase 39 manual verification tests with real SQLite persistence.

### Changed

- README updated with v4.0 capabilities section and architecture diagram.
- `TemplateRegistry` DELETE endpoint now uses `registry.delete()` instead of accessing private `_templates` dict.
- REQUIREMENTS.md traceability table populated with all 36 v4.0 requirement IDs.
- Configuration reference docs regenerated for new settings.

### Fixed

- E501 line-too-long in `parallel/executor.py`.
- I001 unsorted imports in `service/app.py`.

## [0.1.1] - 2026-04-11

First public PyPI release of `zeroth-core`, the governed multi-agent runtime
library extracted from the Zeroth platform. This release establishes the
OSS-grade metadata, optional dependency extras, and trusted-publisher release
pipeline required for a stable PyPI presence.

### Added

- Apache-2.0 `LICENSE` file at repo root (canonical text).
- `CHANGELOG.md` in keepachangelog 1.1.0 format.
- `CONTRIBUTING.md` with dev setup, PR conventions, issue filing, and license guidance.
- PEP 561 `py.typed` marker under `src/zeroth/core/` — downstream users now receive type hints out of the box.
- `[project.urls]` block in `pyproject.toml` (Homepage, Source, Issues, Changelog).
- PyPI classifiers and keywords for searchability and discoverability.
- `examples/hello.py` — minimal runnable fixture (PKG-06 acceptance) that proves a clean-venv install of `zeroth-core` produces a working program.
- Optional dependency extras carved out of the base dependency list:
  `[memory-pg]`, `[memory-chroma]`, `[memory-es]`, `[dispatch]`, `[sandbox]`, and `[all]` (PKG-03).
- GitHub Actions trusted-publisher release workflow (`release-zeroth-core.yml`) with TestPyPI staging, clean-venv smoke-install gate, and Sigstore attestations (PKG-05).

### Changed

- Dependencies carved into a minimal base plus six optional extras. Installing
  bare `zeroth-core` no longer transitively pulls `psycopg`, `pgvector`,
  `chromadb-client`, `elasticsearch`, `redis`, or `arq` — each backend is
  opt-in via its extra.
- Build backend bumped to `hatchling>=1.27` for PEP 639 SPDX
  `license-expression` support.
- Hatchling wheel target verified for the PEP 420 namespace layout introduced
  in Phase 27. The existing `packages = ["src/zeroth"]` target was kept
  unchanged after confirming it produces a correctly-rooted `zeroth/core/`
  wheel with no stray top-level `zeroth/__init__.py`.

[Unreleased]: https://github.com/rrrozhd/zeroth-core/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/rrrozhd/zeroth-core/releases/tag/v0.1.1

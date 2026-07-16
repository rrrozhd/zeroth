# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

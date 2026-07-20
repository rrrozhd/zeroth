# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.3] - 2026-07-20

### Fixed

Erasure + audit-integrity fixes ported from the public main line (main
versions v0.10.1.5, v0.10.1.10, v0.10.1.14, v0.10.1.16):

- **F1 — erasure left plaintext PII behind.** `redact_run` now clears every
  free-form column that can hold PII: `execution_history` (per-node
  input/output snapshots), `failure_state` (whose message re-derives `error`
  on read, so nulling `error` alone resurfaced the plaintext),
  `condition_results`, `channels`, and `pending_approval` (requester reason +
  metadata). The pre-erasure payload harvest decrypts
  `run_checkpoints.state_json` before parsing — previously every
  at-rest-encrypted deployment hit `JSONDecodeError` and the erasure rolled
  back to a silent no-op. On this tree the fix lands in
  `integrations/persistence/runs/retention_queries.py` (with a `decrypt`
  seam) and `run_repository.py` passes the checkpoint store's
  `decrypt_state_json`.
- **S1 — audit verifier accepted forged erasures.** `erased` is
  digest-excluded and a v2+ erased record's digest folds in stored
  commitments, so a DB-only attacker could flip `erased=True` and rewrite PII
  without breaking digest or signature. The verifier now refuses any record
  claiming to be erased while still carrying populated PII commitment fields
  (`governance/audit/verifier.py`).

## [0.10.2] - 2026-07-20

### Fixed

Tenant-isolation audit fixes ported from the public main line (main versions
v0.10.1.2–v0.10.1.4, v0.10.1.7, v0.10.1.9, v0.10.1.13):

- **F4 — cross-tenant IDOR in the tenant cost/budget API.**
  `GET /v1/tenants/{id}/cost` and `PUT /v1/tenants/{id}/budget` now enforce
  `require_resource_scope`: a principal may only read its own tenant's spend or
  set its own tenant's cap (previously any tenant admin could read or zero-out
  another tenant's budget by path id). Follow-up: `GET
  /v1/deployments/{ref}/cost` is scoped to the served deployment.
- **F8 — cross-tenant IDOR in the webhook API.** Subscriptions are bound to
  the served deployment + tenant (body `deployment_ref`/`tenant_id` are no
  longer trusted); list/get/delete are scoped to the served deployment;
  dead-letter list/replay are scoped via the deployment's own subscription
  set **in the query** (LIMIT applies after the tenant scope), and replay
  carries an ownership guard so a foreign dead-letter reads as 404.
  `WebhookRepository.list_dead_letters` gains a `subscription_ids` IN-clause
  filter; `WebhookService.get_dead_letter` is exposed for the guard.
- **F7 — missing RBAC gate on the econ-plane costing router.** Pricing
  catalog and cost-profile writes are Admin-only; reads require any econ role
  — matching every sibling econ router (previously the router carried no auth
  dependency at all).

## [0.10.1] - 2026-07-20

### Added

- **Console F-series wave + econ portfolio dashboard, ported from the public
  main line** (main versions v0.10.0.0.4–v0.10.1.1). The frontend adopts the
  console audit wave wholesale: attestation panel + deployment rollback
  (F12/F6), run controls + quality-verdict + contract form (F3/F5/F11),
  tenant budget card (F4), evidence export + deployment chain verify (F2),
  Retention & Compliance page (F1), Integrations/webhooks page (F8), and
  Prompt templates page (F9). All backend endpoints these pages call already
  exist on this line; `openapi.json` and `api-types.ts` were regenerated from
  this tree's app and the frontend typechecks clean.
- **Console-reachable econ portfolio dashboard via proxy (F7).** New
  `zeroth.service.api.econ_dashboard_api` proxies the bundled Regulus
  read-only dashboard views under `/v1/econ/dashboard/*` behind
  `METRICS_READ`, using the same server-side self-auth bridge as `cost_api`.
  Registered on both the `/v1` and compat routers; covered by
  `tests/service/test_econ_dashboard_api.py`. The refactor-era contract
  snapshots (`backend_openapi.json`, `backend_route_inventory.json`) are
  regenerated for the 9 additive routes — a deliberate surface extension, not
  drift.

### Removed

- Dead econ-plane statistics router (F14, main v0.10.0.1.1): the 3-line
  `statistics/api.py` stub and its two `main.py` registration lines. The
  `statistics.schemas` models (pinned surface) are untouched.

## [0.10.0.1] - 2026-07-19

### Fixed

- `AsyncSQLiteDatabase.transaction()` no longer fails with a spurious
  `CoordinationTimeoutError` when several fresh connections race the one-time
  delete→WAL journal-mode conversion on a new database. SQLite's
  deadlock-avoidance path returns `SQLITE_BUSY` from
  `PRAGMA journal_mode = WAL` without consulting the busy timeout, so the
  pragma is now retried within the coordination-timeout budget. Coordination
  semantics (`BEGIN IMMEDIATE` write lock, `CoordinationTimeoutError`
  contract) are unchanged.

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.1.1] - 2026-07-23

### Fixed

- `govern_graph` transparency and callback-merge hardening (ZER-2 audit-1):
  - `GovernedGraph.with_config(...)` now stays governed. Previously it delegated
    through `__getattr__` and handed back a bare, ungoverned `RunnableBinding`,
    silently dropping governance. It now returns a governed wrapper that binds the
    config into every run while preserving attribute delegation and the
    `on_run_start` seam. `|` composition raises a clear, actionable error.
  - Callback-merge dedup keys on governance-handler **type/identity**, never on
    user-callback equality. Nested `govern_graph(govern_graph(g))` or a
    pre-installed handler no longer double-installs governance, and a user
    callback with a hostile `__eq__` can no longer suppress the Zeroth handler
    (both list and `BaseCallbackManager` config shapes).
  - Econ telemetry emission in `InstrumentedLangGraph` is now best-effort: a
    failing transport can never replace the graph's result, mask its exception,
    or alter cancellation.

## [0.12.1] - 2026-07-23

### Added

- `govern_graph`, a transparent observed-mode wrapper around a compiled
  LangGraph, exported from `zeroth.integrations.langgraph`. One-line install
  (`graph = govern_graph(graph)`) reuses the econ instrumentation delegation for
  cost capture and merges a Zeroth governance callback handler into each run's
  config without replacing or duplicating user callbacks. Results, streamed
  chunks and exceptions are byte-for-byte equivalent to the bare graph. The
  wrapper honours the FA5 capability floor — it mints no attestation and adds no
  path that promotes a run above `admission` (promotion to `observed` is
  deferred) — and exposes an optional no-op `on_run_start` stability seam.
  Importing the package never imports the optional `langgraph` dependency.

## [0.12.0] - 2026-07-21

### Added

- Rebuilt operations console, including Studio, deployment, governance,
  optional Regulus views, and destination-only Webhooks integration.
- Platform-admin-only, allowlisted Regulus proxy with deterministic generated
  platform and Regulus API clients.

### Changed

- The durable structured-token engine is now selected for unauthored graphs.
  Explicit `sequential_join_enabled=False` remains the temporary warned legacy
  compatibility escape hatch; immutable deployment engine pins still win.

### Fixed

- Structured loop boundary delivery, nested lifecycle cancellation, graceful
  stop, replay, checker topology/schedule coverage, and deployment pinning
  release blockers.

## [0.11.1] - 2026-07-20

### Added

- **B9 token/provenance join engine (P1–P3, flag-off), ported from
  `feat/b9-join-loops`** (branch versions v0.11.5.2–v0.11.6.1 + subsequent
  oracle/replay/diamond commits), reimplemented on the decomposed runtime.
  Replaces the loop-epoch join model with provenance tags that travel with
  the token:
  - `runtime/orchestration/token_scope.py` (new): pure static loop analysis —
    DFS back-edges, natural-loop bodies, enclosing loops, exit edges and
    their outermost-owner units, tag propagation.
  - `GraphDriver`: tag-keyed join buckets (per-iteration re-join on loops),
    back-edge re-entry dispatch, loop-exit edges resolved only by the
    exit-crossing event (whole unit at the outer tag), skip cascade with
    dead-loop exit suppression, durable in-flight dispatch records
    (stage/restore) so a failed node replays its exact payload and tag, and
    declared-shape JoinConfig merges (`collect` default, `merge_path`).
  - Validation: `IRREDUCIBLE_LOOP`, `MULTI_LATCH_LOOP`, `FANOUT_IN_LOOP`
    (parallel-config-in-loop + structural token forks with the
    reconvergence-before-boundary proof), `FANOUT_SUCCESSOR_JOIN`; the
    `JOIN_ON_CYCLE` rejection is removed — loops now join correctly
    (`contracts/graph/validation/token_loops.py`, rewritten `joins.py`).
  - Models: `ParallelConfig` gains `max_concurrency`/`batch_size`/
    `branch_timeout_seconds` (wave + worker-pool + per-branch timeout
    execution in `runtime/parallel/executor.py`); `JoinConfig` default flips
    to non-lossy `collect` and gains `merge_path`.
  - The monolith's overridable orchestrator seams are preserved through the
    decomposition: `_dispatch_node`, `_record_forward_resolution`,
    `_stash_join_payload`, `_merge_join_payloads`, `_back_edge_ids` route
    driver-internal calls through the facade so subclasses (incl. the
    trace/oracle bridge) observe the real engine. Loop-analysis caches live
    on the facade instance and are threaded into each driver.
  - Test suites ported: extended `test_join_barrier{,_stress}.py` (nested
    loops, loop-then-combine, multi-exit, bypassed-loop shapes), new
    `test_token_scope.py`, `test_token_engine_model.py` (reference oracle),
    `test_token_engine_runtime_trace.py` (real-runtime trace bridge incl.
    seeded corruption), `test_failed_node_replay.py`; parallel executor
    batching/concurrency/timeout suites. Design spec copied to
    `docs/superpowers/specs/`.
  - Surface fixtures re-amended additively (`ParallelConfig`, `JoinConfig`,
    `RuntimeOrchestrator` cache fields).

## [0.11.0.0.1] - 2026-07-20

### Fixed

- Formatting-only: collapse a wrapped list comprehension in
  `runtime/context/tracker.py` (ruff format; missed in the v0.10.6 port
  commit).

## [0.11] - 2026-07-20

### Added

- **B9 — sequential join barrier (feature-flagged, default OFF), ported from
  the public main line** (main v0.11 `924ab54` + v0.11.0.1 deadlock guard
  `d012200`), reimplemented on the decomposed runtime. An opt-in dispatch
  subsystem fixing diamond payload corruption: an unconditional convergent
  node previously executed twice and clobbered its merged input. Gated by
  `ExecutionSettings.sequential_join_enabled` (default False — flag-off
  behavior byte-identical, verified by the unchanged characterization pins).
  - New `JoinConfig` model (same merge vocabulary + reducer registry as
    `ParallelConfig`) and `NodeBase.join_config`
    (`contracts/graph/models.py`).
  - Publish validation: `MISSING_JOIN_CONFIG` on genuine concurrent delivery
    without a merge policy; `JOIN_ON_CYCLE` rejects convergent-on-cycle
    graphs (new `contracts/graph/validation/joins.py`, wired into the
    contract validator after cycle checks).
  - `GraphDriver` gains the barrier: `run_branch_planner` (full plan with
    suppressed edges), `edge_payload` (shared payload/mapping computation),
    `advance_downstream` (single sequential post-node entry point), and the
    join worklist (delivered/suppressed edge resolution, skip cascade,
    `join_state` checkpoint round-trip, JoinConfig merge, cyclic-edge
    defense). The approval-resolution path advances through the same entry
    point.
  - Deadlock guard: leftover `join_state` at completion fails the run loudly
    (`join_deadlock`) instead of silently completing past a join waiting on
    an unreachable inbound edge.
  - Test suites ported: `tests/orchestrator/test_join_barrier.py` (24 tests,
    incl. the documented shared-schema merge-clobber xfail) and
    `test_join_barrier_stress.py`.
  - **Surface fixture amendment (first use of the additive-amendment policy,
    now documented in `docs/backend-library-surface.md`):**
    `ExecutionSettings` and `NodeBase` (+ its six node subclasses) gain the
    new optional fields in both surface fixtures — 32 signature updates plus
    the `JoinConfig` registration on both surfaces; the legacy shim
    `zeroth.core.graph.models` re-exports `JoinConfig`.

## [0.10.6] - 2026-07-20

### Fixed

B-series orchestrator/infra fixes ported from the public main line (main
v0.10.1.17, v0.10.1.19, v0.10.1.20, v0.10.1.23, v0.10.1.24, v0.10.1.25.1) —
this completes the port of main's entire 29-release `0.10.1.x` audit series:

- **B5/B6/B7 — econ-plane query 500s + dollar-denominated counterfactual**
  (`econ/plane/{capabilities,counterfactual,performance}/service.py`), with
  the new `tests/econ_plane/test_service_query_fixes.py` suite.
- **B11 — sidecar no longer double-applies network config**
  (`integrations/sandbox/executor.py`).
- **B12 — Vault token refresh** on expiry with single-flight re-login
  (`platform/secrets/vault.py`).
- **B13 — signal handling left to uvicorn.** The lifespan's
  `loop.add_signal_handler` block overrode uvicorn's own SIGTERM/SIGINT
  handlers, so `should_exit` never set and the process hung until SIGKILL
  (`service/bootstrap/lifecycle.py`).
- **B1/B3 — prompt no longer re-renders audit; context tracker counts tool
  calls** (`runtime/agents/prompt.py`, `runtime/context/tracker.py`).
- **B8 — nested approval pause on a RESUMED fan-out branch persists
  durably** via the same pause handler as the first pause, instead of an
  in-memory re-queue that was lost on reload (`runtime/orchestration/
  driver.py`).
- **B10 — best_effort fan-out fails loud on multi-branch approval pause**
  (new `MultipleBranchPauseError`) instead of silently orphaning all but the
  last paused branch (`runtime/parallel/{errors,executor}.py`).

## [0.10.5] - 2026-07-20

### Fixed

Security-hardening fixes ported from the public main line (main v0.10.1.12,
v0.10.1.15, v0.10.1.18, v0.10.1.20.1, v0.10.1.21, v0.10.1.22):

- **S5 — condition evaluator blocks dunder attribute access** on non-Mapping
  objects, closing the `__class__`/`__globals__` introspection gadget path
  (`contracts/conditions/evaluator.py`).
- **S7 — subgraph resolution is tenant-scoped.** `SubgraphResolver.resolve`
  (and the `DeploymentLookup` protocol seam) take `tenant_id`/`workspace_id`;
  all four call sites (executor initial + resume, driver resume path,
  parallel-executor fallback) pass the parent run's tenant, so a subgraph
  node naming a foreign tenant's deployment ref fails closed.
- **S3/S4 — rate-limit and quota check-and-update serialized.** Token-bucket
  and quota transactions take `write_lock=True` (+ `FOR UPDATE` on Postgres);
  cold-start insert uses `ON CONFLICT DO NOTHING` + re-read, so concurrent
  first requests can't 500 or double-spend a capacity-1 bucket
  (`governance/guardrails/rate_limit.py`).
- **S2 — strict sandbox refuses the local backend unconditionally** (was a
  silent no-op for bare inline units with no resource constraints); STANDARD
  behavior unchanged (`integrations/execution/sandbox.py`).
- **B4 — run worker never leaks a concurrency slot.** The lease-renewal task's
  own exception (e.g. "database is locked") no longer escapes the `finally`
  before `release_lease` + semaphore release
  (`runtime/orchestration/run_worker.py`).
- **S6 — failure text routed through the secret redactor.** Both persisted
  audit `error` columns and `RunFailureState.message` (returned verbatim by
  the public run API) are redacted; no-op without a secret resolver
  (`runtime/orchestration/{audit_recorder,driver}.py`).

## [0.10.4] - 2026-07-20

### Fixed

- **F3 — cooperative run cancellation, full series ported from the public
  main line** (main v0.10.1.6, v0.10.1.8, v0.10.1.11, v0.10.1.25). The drive
  loop holds an in-memory `Run` and blind-writes `RUNNING` every hop, so an
  operator's out-of-band cancel (`FAILED`) or interrupt (`WAITING_INTERRUPT`)
  was clobbered and the run drove to completion. `GraphDriver.external_stop`
  re-reads the persisted status at the loop head and before every `RUNNING`
  write (ordinary nodes, sync/resumed subgraphs, parallel and resumed
  fan-ins); it adopts the operator's status onto the in-memory run and
  persists it (so `pending_node_ids` survives for replay), and yields to a
  concurrent operator replay/resume rather than blind-writing the stale
  status back. The refactor-era characterization pins gain the new
  `run.get` observations — a deliberate contract change, not drift.

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

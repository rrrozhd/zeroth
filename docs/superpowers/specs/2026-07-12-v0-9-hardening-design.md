# Zeroth v0.9 Hardening Design

**Status:** Approved in conversation on 2026-07-12

**Goal:** Close the release-blocking correctness, isolation, concurrency, retention,
MCP, Vault, documentation, formatting, and test-coverage gaps found in the v0.9
audit while deferring broad orchestrator and Studio decomposition to a documented
follow-up.

## Scope and delivery shape

This is one release-hardening objective composed of six independently testable
workstreams. Implementation planning must keep them as separate plans/commit units
and integrate them only after each workstream is green:

1. runtime dispatch isolation;
2. tenant-safe deployment operations;
3. cross-worker audit and retention coordination;
4. retention-policy correctness;
5. MCP and Vault runtime hardening;
6. product/release truth, formatting, and frontend regression coverage.

The large-scale decomposition of `RuntimeOrchestrator` and the Studio editor is
explicitly out of implementation scope. It will be captured in a standalone
architecture roadmap with extraction boundaries, sequencing, and acceptance gates.

## 1. Runtime dispatch isolation

### Problem

`RuntimeOrchestrator` currently mutates shared `AgentRunner` instances per dispatch:
the provider, config, memory resolver, budget enforcer, context tracker, and tool
executor are temporarily replaced and restored. Concurrent runs can interleave those
mutations and leak prompts, wrappers, tenant attribution, or tool state.

### Design

Add an explicit `AgentRunner.fork_for_dispatch()` operation. It returns a
dispatch-local runner that:

- shares immutable/heavy dependencies that are safe to share, including the base
  provider client and contract models;
- owns its mutable config reference, provider-wrapper chain, tool bridge/registry,
  MCP manager, context tracker, resolver injection, budget-enforcer injection, and
  tool executor;
- starts with no live MCP session and closes only the sessions it started;
- never writes dispatch-local state back to the registered prototype runner.

The orchestrator will fork immediately after runner lookup and apply all temporary
configuration to the fork. Restoration logic becomes unnecessary for prototype
state; cleanup remains in `finally` for dispatch-owned resources.

### Acceptance

Two deliberately interleaved runs using the same registered runner must retain their
own instruction, provider wrapper, tenant/cost attribution, memory context, tool
executor, and cleanup. The registered prototype must be unchanged after both finish.

## 2. Tenant-safe deployment operations

### Problem

Studio stamps `Graph.tenant_id`, but deployment creation derives the tenant from the
unrelated `deployment_settings` dictionary. Deployment create/rollback/list API paths
also fail to consistently scope operations to the authenticated principal.

### Design

`Graph.tenant_id` plus a new optional `Graph.workspace_id` become the authoritative
deployment owner scope. Migration `009` adds the graph workspace column and leaves
legacy rows as `NULL`; their serialized payloads hydrate through the model default of
`None`. A legacy graph is accessible only to a principal whose workspace is also
`None`—`NULL` is not a tenant-wide wildcard. Studio stamps both fields from the
authenticated principal for every new graph.

- `DeploymentService.deploy` accepts an optional required tenant/workspace scope for
  service/API calls. It loads the graph through that exact scope and stamps the new
  deployment from the graph owner scope.
- Internal code-authored callers may omit the scope; the graph's persisted owner is
  still used and never replaced by `deployment_settings`.
- Lineage checks, version allocation, create, get, list, and rollback all carry the
  same tenant+workspace scope.
- Deployment API endpoints pass `principal.tenant_id` and
  `principal.workspace_id`; listings are scoped by the principal, not the serving
  deployment. A service with no active deployment never falls back to an unfiltered
  list.
- Foreign-tenant graph IDs and deployment refs return 404 without existence
  disclosure.

### Acceptance

A Studio graph owned by tenant/workspace A deploys in that exact scope without hidden
settings. Another tenant or workspace cannot create from it, list it, roll it back, or
supersede it. Deployment references remain globally unique for backward compatibility;
an attempted collision with a foreign scope returns the existing not-found-style
response without mutating or disclosing the owner.

## 3. Cross-worker audit and retention coordination

### Problem

Audit-chain writes use only an in-process lock. Multiple workers can append from the
same head and fork a run's chain. Retention erasure checks legal holds once and then
performs several transactions, so hold placement can race destructive work.

### Design

Introduce database-backed coordination behind small protocols rather than embedding
dialect checks throughout repositories.

### Audit chain

- Add migration `010` with an `audit_chain_heads` row per run.
- Add an `AuditChainCoordinator` with SQLite and Postgres implementations.
- SQLite acquires a write transaction before reading/updating the head; Postgres locks
  the run's head row with `SELECT ... FOR UPDATE`.
- Record insertion and head advancement happen in the same transaction. A failed
  insert rolls back the head. Existing chains lazily initialize their head from the
  latest stored audit.
- The process-local lock may remain as a contention optimization, but correctness must
  not depend on it.

### Retention/legal hold

- Add one coordination row keyed only by tenant in migration `010`. Every legal-hold
  placement/release (tenant-wide or run-specific) and every database-destructive
  erasure for that tenant acquires this same row. There are no nested tenant/run locks,
  so there is no lock-order ambiguity; the deliberate trade-off is serializing
  retention administration within a tenant.
- Under that lock, re-read active holds and perform audit tombstoning, checkpoint
  deletion, and run redaction atomically.
- Artifact and economic-plane deletion occur after the database transaction and remain
  idempotent/best-effort. Their status is recorded explicitly; they do not roll back
  already-authorized database erasure.
- A hold request concurrent with erasure must serialize deterministically: whichever
  acquires the lock first completes, and the later operation observes the resulting
  state. No operation may pass a stale pre-lock hold check.

### Acceptance

Concurrent repository instances writing the same run produce one continuous chain on
SQLite and Postgres. Concurrent hold/erase tests prove there is no stale-check window.

## 4. Retention-policy correctness

### Problem

`run_ttl_seconds` and configured defaults are inert; `audit_ttl_seconds` can erase a
whole run based on one old audit; invalid non-positive TTLs are accepted.

### Design

- Validate configured and API TTLs as positive integers when present.
- `audit_ttl_seconds` tombstones only individual v2 audit records older than the audit
  cutoff. It does not delete checkpoints or redact a run.
- `run_ttl_seconds` selects terminal runs whose persisted `updated_at` is older than
  the run cutoff and performs full-run erasure. Active, pending, and
  waiting-for-approval runs are never TTL-erased.
- Tenant-wide and run-specific legal holds apply to both surfaces.
- `RetentionSettings.default_*_ttl_seconds` become the authoritative system-default
  policy at bootstrap. Only the absence of a tenant policy triggers inheritance.
  Within an explicit tenant policy, a `null` TTL means "keep this surface forever"
  and does not inherit that field from the system default.
- Repository queries return IDs/metadata needed for the sweep instead of loading and
  deserializing every audit only to query each run again.

### Acceptance

Tests cover independent audit/run TTLs, mixed-age audits, active runs, default policy
configuration, non-positive validation, legal holds, idempotency, and bounded query
behavior.

## 5. MCP and Vault runtime hardening

### MCP

- The automatic runner factory copies `AgentNodeData.mcp_servers` into
  `AgentConfig`.
- Starting an MCP stdio server requires both `PROCESS_SPAWN` and
  `EXTERNAL_API_CALL` when capability enforcement is active. The check runs before
  constructing `MCPClientManager` or spawning a process.
- Graph validation rejects an MCP-configured agent that does not declare both
  capabilities. When enforcement is deliberately disabled, existing advisory behavior
  remains available and documented.
- Discovered MCP tools retain the same capability requirements at call time.

### Vault

- Add async secret-resolution methods and a shared async helper used by async bootstrap,
  runner construction, signing construction, HTTP auth, and execution paths.
- `VaultSecretProvider` owns a reusable `httpx.AsyncClient`, uses a per-key async lock
  to collapse concurrent cache misses, and provides `aclose()` for service shutdown.
- Environment-backed resolution remains direct and cheap. Synchronous compatibility is
  retained only for genuinely synchronous callers and must not run on the service event
  loop.
- AppRole token acquisition is also single-flight and uses the shared client.

### Acceptance

Factory tests prove MCP configuration survives graph-to-runner construction. Capability
tests prove the process is never started on denial. Vault tests prove concurrent misses
issue one request, repeated hits reuse the client/cache, and async service paths do not
call synchronous HTTP.

## 6. Product truth, release hygiene, formatting, and frontend tests

### 6a. Documentation and release metadata

- Correct the README canvas-to-run wording: deployment creation requires a serving
  process restart/reload before the new version can run.
- Describe budget enforcement as default-enabled when the bundled `regulus` extra is
  installed; bare installs fail open unless an external plane is configured.
- Update `.planning/PROJECT.md` to the actual Next.js, embedded-econ-plane, v0.9 state
  and distinguish package versions (`0.x`) from historical planning milestones
  (`v4.x`).
- Add a complete 0.9 hardening entry to `CHANGELOG.md` and prepare version `0.9.1` as
  the bug-fix release. Do not tag, push, or publish without separate authorization.

### 6b. Formatting

- Run Ruff formatting across `src/` as a separate mechanical change after behavioral
  work is green.

### 6c. Frontend and warning regression coverage

- Add a minimal frontend test runner and focused tests for deployment/run eligibility
  helpers extracted from the Studio page. Do not attempt broad component coverage in
  this pass.
- Fix the unawaited-coroutine warning in `tests/dispatch/test_worker.py` with a
  regression test or corrected fixture behavior.

### 6d. Deferred decomposition document

Create `docs/architecture/runtime-studio-decomposition.md` covering:

- extraction seams for dispatch preparation, agent execution context, audit emission,
  parallel/subgraph coordination, Studio editor state, deployment modal, run panel,
  and node inspector;
- dependency direction and stable interfaces;
- incremental phases, characterization-test gates, and rollback strategy;
- explicit non-goals so the future refactor does not change runtime behavior.

## Error handling and compatibility

- New tenant-scope failures use existing 404 hiding conventions.
- Database lock acquisition has a bounded retry/timeout and surfaces a specific
  coordination error; it never silently falls back to process-only correctness.
- Existing unsigned audit rows and digest-v1 rows remain readable/verifiable.
- Existing code-authored default-tenant graphs remain deployable.
- Retention external-cleanup failures remain visible in the retention audit log and can
  be retried idempotently.
- Migration `010` downgrade removes only the coordination tables/indexes and does not
  rewrite existing audit or retention records. Migration `009` downgrade removes the
  dedicated graph workspace column using the backend's supported batch-table path;
  serialized legacy graph payloads remain valid because `workspace_id` is optional and
  absent payloads hydrate as `None`. Upgrade→downgrade→upgrade is covered on SQLite and
  Postgres.

## Verification strategy

Every behavior change follows red-green TDD. Required final gates:

1. focused regression tests for every audit finding;
2. SQLite migration and concurrency tests;
3. Postgres integration tests when Docker/testcontainers is available, otherwise an
   explicit unverified-environment report;
4. full `uv run pytest -q`;
5. `uv run ruff check src/` and `uv run ruff format --check src/`;
6. frontend unit tests and `npm run build`;
7. `uv build --wheel` plus clean-wheel import/CLI smoke test;
8. `git diff --check` and confirmation that pre-existing unrelated edits were not
   included.

## Success criteria

The pass is complete when all release-blocking findings have executable regression
coverage, the implementations satisfy those tests across supported databases, product
claims match shipped behavior, verification gates are green, and the deferred large-file
refactor has an actionable but unimplemented architecture roadmap.

# Backend Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize Zeroth's backend into explicit acyclic domains, decompose its largest modules, preserve service and library behavior, and remove only proven unreachable or superseded code.

**Architecture:** Introduce canonical `runtime`, `governance`, `platform`, `contracts`, `service`, `econ`, `integrations`, and `eval` packages. Move one dependency layer at a time, keeping runtime contracts separate from integration implementations. Large public facades retain behavior while delegating to focused collaborators.

**Tech Stack:** Python 3.12, Pydantic, FastAPI, asyncio, SQLAlchemy/database adapters, pytest, Ruff, uv, AST-based architecture checks.

**Design:** `docs/superpowers/specs/2026-07-18-backend-architecture-refactor-design.md`

---

## Global execution rules

- Work only in `/Users/dondoe/coding/zeroth/.worktrees/backend-architecture-refactor`.
- Do not modify `frontend/`.
- Before each production edit, add or identify a focused characterization test and observe the new boundary test fail.
- After every task, run the listed tests and Ruff, then create the listed atomic commit.
- Update this plan's checkboxes as work completes; checkbox-only updates may accompany the relevant task commit.
- Use `git diff --exit-code 01b36a9 -- frontend/` after each phase to guard frontend scope.
- Never delete a public or optional-integration symbol based only on static call counts.
- Golden canonical-fixture and migration-guide changes never share a commit with production moves. Use non-golden import/boundary tests for red-green, commit passing implementation first, then update `backend_surface_canonical.json` and its migration row in a separate passing `docs: record <domain> import migration` commit.

### Task 0: Re-establish the all-extras baseline

**Files:**
- Create: `docs/backend-refactor-baseline.md`

- [x] Run `uv sync --all-extras`; expect exit 0.
- [x] Run `uv run pytest -q`; expect exactly `1973 passed, 16 deselected, 3 warnings` before implementation.
- [x] Run `uv run python -c "import chromadb, elasticsearch, psycopg; from zeroth.core.memory.chroma_connector import ChromaDBMemoryConnector; from zeroth.core.memory.elastic_connector import ElasticsearchMemoryConnector; from zeroth.core.memory.pgvector_connector import PgvectorMemoryConnector"`; expect exit 0.
- [x] Record the commands, environment, totals, deselections, and known warnings in the baseline document.
- [x] Run `git diff --exit-code 01b36a9 -- frontend/`; expect exit 0 and no output.
- [x] Commit: `docs: record backend refactor baseline`.

### Task 1: Capture protected contracts and library inventory

**Files:**
- Create: `docs/backend-library-surface.md`
- Create: `docs/backend-import-migration.md`
- Create: `tests/architecture/test_library_surface.py`
- Create: `tests/contracts/test_refactor_contract_snapshots.py`
- Create: `tests/contracts/fixtures/backend_openapi.json`
- Create: `tests/contracts/fixtures/backend_surface_legacy.json`
- Create: `tests/contracts/fixtures/backend_surface_canonical.json`
- Create: `tests/contracts/fixtures/database_schema.json`
- Modify: `tests/conftest.py` only if stable snapshot normalization needs a shared fixture

- [x] Enumerate public symbols from `__all__`, package exports, docs, examples, entry points, schema models, and optional integrations into immutable `backend_surface_legacy.json`, evolving `backend_surface_canonical.json`, and the human-readable inventory.
- [x] Add one test that verifies immutable legacy capabilities/signatures and a separate smoke test that imports every evolving canonical symbol and checks `inspect.signature` for callables.
- [x] Add semantic snapshots for OpenAPI paths/components, Alembic revision order, actual SQLite schema tables/columns/indexes/constraints, persisted model round trips, and representative public exceptions.
- [x] Require every canonical fixture edit to be isolated with its migration-guide row; never edit the legacy fixture after this task.
- [x] Run the snapshot tests and confirm they pass against the baseline; these are characterization tests rather than new behavior.
- [x] Run `uv run pytest tests/architecture/test_library_surface.py tests/contracts/test_refactor_contract_snapshots.py -v`.
- [x] Run `uv run ruff check tests/architecture tests/contracts`.
- [x] Commit: `test: capture backend refactor contracts`.

### Task 2: Enforce dependency direction

**Files:**
- Create: `tests/architecture/test_backend_dependencies.py`
- Create: `src/zeroth/_architecture.py`

- [x] Write a failing repository-wide AST test defining the exact dependency matrix and scanning every Python file under `src/zeroth`; confirm it fails on current unclassified edges.
- [x] Implement a scanner in `_architecture.py` that normalizes absolute and relative imports, reports importer file/line and imported module, and supports exact `(importer, imported)` temporary exceptions.
- [x] Seed narrowly scoped current-edge exceptions; every entry must include a reason and exact removal task. Keep the real-tree assertion enabled.
- [x] Run `uv run pytest tests/architecture/test_backend_dependencies.py -v`.
- [x] Run `uv run ruff check src/zeroth/_architecture.py tests/architecture`.
- [x] Commit: `test: enforce backend dependency direction`.

### Task 3: Introduce canonical package skeletons and migration guide

**Files:**
- Create: `src/zeroth/{runtime,governance,platform,contracts,service,econ,integrations,eval}/__init__.py`
- Create nested package `__init__.py` files listed in the design's authoritative tree
- Modify: `docs/backend-import-migration.md`
- Modify: `tests/architecture/test_library_surface.py`

- [x] Add failing imports for every canonical top-level and nested package in the design; run the surface test and confirm `ModuleNotFoundError` for the first missing package.
- [x] Create empty package skeletons with narrow docstrings and no eager cross-domain imports.
- [x] Add only the migration-table schema in this production skeleton commit.
- [x] Run `uv run pytest tests/architecture -v`.
- [x] Run `uv run ruff check src/zeroth tests/architecture`.
- [x] Commit production skeletons: `refactor: establish backend domain packages`.
- [x] Add initial unchanged/move rows, run the surface test, and commit separately: `docs: initialize backend import migration`.

### Task 4: Consolidate clocks and identifier primitives

**Files:**
- Create: `src/zeroth/platform/primitives/{__init__.py,clock.py,identifiers.py}`
- Create: `tests/platform/test_primitives.py`
- Modify identical helpers one domain at a time in approvals, audit, conditions, deployments, graph, guardrails, runs, retention, thread store, and webhooks

- [x] Add failing tests for timezone-aware UTC values, injected clock behavior, and UUID string generation.
- [x] Implement `utc_now()`, a `Clock` protocol, `SystemClock`, and `new_uuid()` without domain-specific formatting.
- [x] Run `uv run pytest tests/platform/test_primitives.py -v` and `uv run ruff check src/zeroth/platform/primitives tests/platform/test_primitives.py`; commit `refactor: add platform primitives` before migrating consumers.
- [x] For each domain, add a failing monkeypatch/injection test proving it consumes the canonical helper, replace only equivalent helpers, run that domain's tests and Ruff, then commit `refactor: use platform primitives in <domain>`.
- [x] Run `uv run pytest tests/platform/test_primitives.py tests/approvals tests/audit tests/conditions tests/runs tests/retention tests/test_webhook_models.py tests/test_webhook_repository.py -q` as the combined gate.
- [x] Run `uv run ruff check src/zeroth/platform src/zeroth/core` and the frontend diff guard.

### Task 5: Establish runtime run-domain contracts

**Files:**
- Create: `src/zeroth/runtime/runs/{__init__.py,models.py,protocols.py}`
- Create: `tests/runtime/test_run_contracts.py`
- Modify: orchestrator, agent runtime, approval, dispatch, retention, and service imports of run/thread models and protocols
- Modify: `docs/backend-import-migration.md`

- [x] Add failing canonical import and signature tests for run/thread/checkpoint models and repository protocols.
- [x] Move models without changing Pydantic fields, defaults, serialization, enums, or exception behavior.
- [x] Define narrowly named `RunReader`, `RunWriter`, `CheckpointStore`, and `ThreadStore` protocols from actual runtime consumption. Preserve the complete public `RunRepository` and `ThreadRepository` APIs and their immutable legacy signatures.
- [ ] Rewrite runtime-facing imports to `zeroth.runtime.runs`. **Deferred:** while the
      canonical package re-exports models defined under `zeroth.core`, no module reachable
      from `zeroth.core.__init__` can import it without closing an import cycle. Each
      consumer is repointed as its own package leaves `zeroth.core` (Tasks 10-16). See
      the import-direction constraint in `docs/backend-import-migration.md`.
- [x] Run `uv run pytest tests/runs tests/orchestrator tests/agent_runtime tests/dispatch tests/approvals -q`.
- [x] Run `uv run pytest tests/contracts/test_refactor_contract_snapshots.py tests/architecture/test_library_surface.py -v` and `uv run ruff check src/zeroth/runtime src/zeroth/core tests/runtime`.
- [x] Commit: `refactor: define runtime run contracts`.

### Task 5.5: Make the platform layer import-clean (inserted 2026-07-18)

**Not in the original plan.** Added because Task 6 could not proceed without
it, and Tasks 7-16 depend on it for the same reason.

Importing `zeroth.core.storage` -- the bottom of the dependency matrix --
executed 130 modules including `zeroth.core.runs.repository`, `agent_runtime`,
`audit`, and `memory`. Any module the repository imports was therefore loaded
while `zeroth.core` was still initializing, so extracting a concrete adapter
into its own package produced a circular import in both directions. The static
dependency matrix could not see this: it checks which modules *name* each
other, not what the interpreter actually executes.

- [x] Enumerate every module-level route from the platform layer into higher
      domains (seven edges from two modules).
- [x] Resolve `zeroth.core.econ` and `zeroth.core.http` exports lazily so
      `config.settings` can read one settings model without loading the
      package's whole public API.
- [x] Move the governed-store imports in `zeroth.core.storage.redis` into the
      factory that builds them.
- [x] Resolve `RunRepository`/`ThreadRepository` lazily in `zeroth.core.runs`.
- [x] Add `tests/architecture/test_import_layering.py`, which asserts the
      closure from subprocesses; the in-process suite always has `zeroth.core`
      warm via `tests/conftest.py` and structurally cannot catch this.
- [x] Commits: `refactor: keep the platform layer out of higher domains`,
      `refactor: resolve run repositories lazily`.

Result: 130 modules down to 18. **Do not make these package `__init__` files
eager again** -- that re-blocks Task 6 onward. Full analysis, the four permitted
leaf imports that remain, and a Python name-collision subtlety in
`zeroth.core.econ` that is not obvious from the code:
`docs/backend-refactor-eager-import-blocker.md`.

Applies to the remaining move tasks: a package moved out of `zeroth.core` must
stay cold-importable. Each canonical package needs a subprocess cold-import
guard as it is published, since the warm-cache suite cannot see the failure.

### Task 6: Split concrete run persistence

**Files:**
- Create: `src/zeroth/integrations/persistence/runs/{__init__.py,serialization.py,checkpoint_store.py,run_repository.py,thread_repository.py,retention_queries.py}`
- Create: `tests/integrations/persistence/runs/`
- Replace: `src/zeroth/core/runs/repository.py`
- Modify: service/bootstrap wiring and persistence imports
- Modify: `docs/backend-import-migration.md`

- [x] Add failing imports for `serialization` and `checkpoint_store`; run `uv run pytest tests/integrations/persistence/runs/test_serialization.py tests/integrations/persistence/runs/test_checkpoint_store.py -v` and confirm missing-module failures.
- [x] Extract serialization and checkpoint storage; rerun that exact command, Ruff the two source/test modules, and commit `refactor: extract run serialization and checkpoints`.
- [x] Update the canonical fixture/migration row, rerun surface/snapshot tests, and commit separately: `docs: record run serialization migration`.
- [x] Add failing imports and characterization tests for transaction scope, transitions, row conversion, and erasure queries; run the new `test_run_repository.py` and confirm missing-module failures.
- [x] Extract run persistence and retention queries; rerun `uv run pytest tests/integrations/persistence/runs/test_run_repository.py tests/runs -q`, Ruff affected files, and commit `refactor: extract run persistence adapter`.
- [x] Add a failing canonical import plus thread create/get/update/resolve tests, extract thread persistence, run `uv run pytest tests/integrations/persistence/runs/test_thread_repository.py tests/runs -q`, Ruff, and commit `refactor: extract thread persistence adapter`.
- [x] Update bootstrap injection and remove the legacy repository implementation after import inventory proves no remaining canonical consumers.
      **Partially deferred:** the import inventory proved the opposite of its
      precondition. `zeroth.core.runs` must keep republishing the adapters
      (`zeroth.core.runs:RunRepository` is a protected legacy capability) until the
      compatibility shell retires in Task 18, and `agent_runtime.thread_store`
      genuinely constructs them — narrowing it to the protocols would rewrite a
      signature pinned in the immutable legacy fixture, so it moves with the agent
      runtime in Task 14. `core/runs/repository.py` is now a 23-line re-export shim
      and a leaf. Reasons recorded in `docs/backend-import-migration.md`.
- [x] Run `uv run pytest tests/runs tests/storage tests/retention tests/service tests/contracts/test_refactor_contract_snapshots.py tests/architecture/test_library_surface.py -q`.
- [x] Run `uv run ruff check src/zeroth/integrations/persistence src/zeroth/runtime/runs src/zeroth/core/service tests/integrations/persistence` and the frontend guard; commit production wiring as `refactor: wire canonical run persistence`, then update the canonical fixture/migration row, rerun surface/snapshot tests, and commit `docs: record run persistence migration` separately.

### Task 7: Decompose graph validation

**Files:**
- Create: `src/zeroth/contracts/graph/validation/{__init__.py,facade.py,nodes.py,edges.py,tools.py,mappings.py,cycles.py,references.py,issues.py}`
- Create: `tests/contracts/graph/validation/`
- Replace: `src/zeroth/core/graph/validation.py`
- Modify: graph validator imports in production slices; migration guide only in separate follow-up commits

- [x] Add a parameterized characterization test asserting exact issue codes, paths, messages, and order for representative invalid graphs; run it and confirm it passes against legacy behavior.
- [x] Add a failing import test for `issues`/`references`, extract them, run `uv run pytest tests/contracts/graph/validation/test_issues_references.py tests/graph -q`, Ruff, and commit `refactor: extract graph validation primitives`.
- [x] Add a failing import test for `nodes`, extract node/entrypoint validation, run its new test plus `tests/graph`, Ruff, and commit `refactor: extract graph node validation`.
- [x] Repeat the red/import, focused pass, Ruff, and atomic commit cycle for `edges`/`tools` and then `mappings`/`cycles`.
- [x] Keep `GraphValidator` as the canonical facade and preserve `validate`/`validate_or_raise` signatures.
- [x] Run `uv run pytest tests/graph tests/contracts/graph/validation tests/contracts/test_refactor_contract_snapshots.py -q`.
- [x] Run `uv run ruff check src/zeroth/contracts/graph tests/contracts/graph`; compose the facade and commit `refactor: compose graph validation facade`.

### Task 8: Decompose orchestration runtime

**Files:**
- Create: `src/zeroth/runtime/orchestration/{__init__.py,facade.py,driver.py,dispatcher.py,parallel_executor.py,policy_gate.py,audit_recorder.py,tool_executor.py,errors.py}`
- Create: `tests/runtime/orchestration/`
- Replace: `src/zeroth/core/orchestrator/runtime.py`
- Modify: orchestrator imports in production slices; migration guide only in separate follow-up commits

- [x] Add characterization coverage for run/resume/failure/audit order/policy/tool/fan-out/pause-resume and confirm it passes against the legacy facade.
- [x] For `audit_recorder`, add a failing collaborator import/injection test, extract minimally, run `uv run pytest tests/runtime/orchestration/test_audit_recorder.py tests/orchestrator -q`, Ruff, and commit `refactor: extract runtime audit recording`.
- [x] For `policy_gate`, repeat red/import/injection, run its test plus `tests/policy tests/approvals`, Ruff, and commit `refactor: extract runtime policy gate`.
- [x] For `dispatcher`/`tool_executor`, repeat red/import/injection, run their tests plus `tests/agent_runtime tests/execution_units tests/rag`, Ruff, and commit `refactor: extract runtime dispatch`.
- [x] For `parallel_executor`, repeat red/import/injection, run its tests plus `tests/parallel tests/subgraph`, Ruff, and commit `refactor: extract parallel runtime`.
- [x] For `driver`, repeat red/import/injection, leave `RuntimeOrchestrator` as composition facade, run `uv run pytest tests/runtime/orchestration tests/orchestrator tests/parallel tests/subgraph tests/policy tests/approvals tests/agent_runtime tests/contracts/test_refactor_contract_snapshots.py -q`, Ruff, and commit `refactor: compose runtime orchestrator`.

### Task 9: Decompose retention erasure

**Files:**
- Create: `src/zeroth/governance/retention/{__init__.py,service.py,manifests.py,replay.py,claims.py,executor.py,compatibility.py,errors.py}`
- Create: `tests/governance/retention/`
- Replace: `src/zeroth/core/retention/erasure_service.py`
- Modify: retention imports in production slices; migration guide only in separate follow-up commits

- [ ] Add characterization tests for claim fencing, heartbeat loss, replay, idempotency, cleanup ordering, manifest completion, and compatibility logs; confirm they pass against legacy behavior.
- [ ] Add failing imports for `manifests`/`replay`, extract minimally, run their new tests plus `tests/retention`, Ruff, and commit `refactor: extract retention cleanup replay`.
- [ ] Add failing imports/injection tests for `claims`, extract minimally, run `uv run pytest tests/governance/retention/test_claims.py tests/retention/test_coordination.py -q`, Ruff, and commit `refactor: extract retention claim coordination`.
- [ ] Add failing imports/injection tests for `executor`/`compatibility`, extract minimally, run their new tests plus `tests/retention`, Ruff, and commit `refactor: extract retention cleanup execution`.
- [ ] Keep `RetentionErasureService` as facade; run `uv run pytest tests/retention tests/governance/retention tests/contracts/test_refactor_contract_snapshots.py -q`, Ruff the retention packages, and commit `refactor: compose retention erasure service`.

### Task 10: Decompose service bootstrap and API ownership

**Files:**
- Create: `src/zeroth/service/bootstrap/{__init__.py,container.py,configuration.py,lifecycle.py,migrations.py,factory.py}`
- Move each `src/zeroth/core/service/*_api.py` to `src/zeroth/service/api/`
- Create: `tests/service/bootstrap/`
- Create: `tests/service/test_authorization.py`
- Create: `tests/service/test_console_ui.py`
- Create: `tests/service/test_health.py`
- Create: `tests/service/test_entrypoint.py`
- Replace: `src/zeroth/core/service/bootstrap.py`
- Modify: application wiring in production slices; migration guide only in separate follow-up commits

**Service file disposition:**

| Current file | Destination/disposition | Focused gate |
| --- | --- | --- |
| `app.py` | `src/zeroth/service/app.py`, thin application composition | `uv run pytest tests/service/test_app.py -q` |
| `auth.py` | `src/zeroth/service/api/authentication.py` | `uv run pytest tests/service/test_auth_api.py tests/service/test_bearer_auth.py -q` |
| `authorization.py` | `src/zeroth/service/api/authorization.py` | `uv run pytest tests/service/test_authorization.py -q` |
| `console_ui.py` | `src/zeroth/service/api/console_ui.py` | `uv run pytest tests/service/test_console_ui.py -q` |
| `health.py` | `src/zeroth/service/api/health.py` | `uv run pytest tests/service/test_health.py -q` |
| `studio_schemas.py` | `src/zeroth/service/api/studio_schemas.py` | `uv run pytest tests/test_studio_api.py -q` |
| `admin_api.py` | `service/api/admin_api.py` | `uv run pytest tests/service/test_admin_api.py -q` |
| `approval_api.py` | `service/api/approval_api.py` | `uv run pytest tests/service/test_approval_api.py -q` |
| `artifact_api.py` | `service/api/artifact_api.py` | `uv run pytest tests/service/test_artifact_api.py -q` |
| `audit_api.py` | `service/api/audit_api.py` | `uv run pytest tests/service/test_audit_api.py -q` |
| `connector_api.py` | `service/api/connector_api.py` | `uv run pytest tests/service/test_connector_api.py tests/service/test_connector_runtime_api.py -q` |
| `contracts_api.py` | `service/api/contracts_api.py` | `uv run pytest tests/service/test_contract_api.py -q` |
| `cost_api.py` | `service/api/cost_api.py` | `uv run pytest tests/test_cost_api.py -q` |
| `deployment_api.py` | `service/api/deployment_api.py` | `uv run pytest tests/service/test_deployment_api.py -q` |
| `econ_analytics_api.py` | `service/api/econ_analytics_api.py` | `uv run pytest tests/test_econ_analytics_api.py -q` |
| `manifest_api.py` | `service/api/manifest_api.py` | `uv run pytest tests/service/test_manifest_api.py -q` |
| `retention_api.py` | `service/api/retention_api.py` | `uv run pytest tests/service/test_retention_api.py -q` |
| `rightsizing_api.py` | `service/api/rightsizing_api.py` | `uv run pytest tests/test_rightsizing_api.py -q` |
| `run_api.py` | `service/api/run_api.py` | `uv run pytest tests/service/test_run_api.py tests/service/test_thread_api.py -q` |
| `studio_api.py` | `service/api/studio_api.py` | `uv run pytest tests/test_studio_api.py tests/service/test_studio_workspace_isolation.py -q` |
| `template_api.py` | `service/api/template_api.py` | `uv run pytest tests/service/test_template_api.py -q` |
| `webhook_api.py` | `service/api/webhook_api.py` | `uv run pytest tests/test_webhook_api.py -q` |
| `entrypoint.py` | `src/zeroth/service/entrypoint.py`, thin process entry point | `uv run pytest tests/service/test_entrypoint.py -q` |
| `__init__.py` | canonical export shell only; no domain implementation | library-surface test |

- [ ] Add characterization tests for dependency identity, injected runners, missing deployments, graph snapshot mismatch, lifecycle cleanup, and exact route inventory; confirm legacy pass.
- [ ] Add failing imports for configuration/migrations, extract, run their new tests plus `tests/service/test_app.py`, Ruff, and commit `refactor: extract service configuration`.
- [ ] Add a failing container import/injection test, extract dependency construction, run its test plus `tests/service/test_app.py`, Ruff, and commit `refactor: extract service dependency container`.
- [ ] Add failing lifecycle/factory imports, extract and move one API module at a time, running the exact command in its disposition row plus OpenAPI snapshot before each atomic `refactor: move <name> service API` commit.
- [ ] For each non-API row in the disposition table, add a failing canonical import/boundary test, move minimally, run its exact focused gate and Ruff, commit production code, then separately update the canonical fixture/migration row and commit the passing docs/fixture change.
- [ ] Run `uv run pytest tests/service tests/contracts/test_refactor_contract_snapshots.py -q`, `uv run ruff check src/zeroth/service tests/service`, and the frontend guard; commit `refactor: compose service bootstrap`.

### Task 11: Move platform packages

**Package slices:**

| Source | Destination | Focused test command | Commit |
| --- | --- | --- | --- |
| `src/zeroth/core/config/` | `src/zeroth/platform/config/` | `uv run pytest tests/test_config.py -q` | `refactor: move config to platform` |
| `src/zeroth/core/storage/` | `src/zeroth/platform/storage/` | `uv run pytest tests/storage tests/test_async_database.py -q` | `refactor: move storage to platform` |
| `src/zeroth/core/artifacts/` | `src/zeroth/platform/artifacts/` | `uv run pytest tests/artifacts -q` | `refactor: move artifacts to platform` |
| `src/zeroth/core/dispatch/` | `src/zeroth/platform/dispatch/` | `uv run pytest tests/dispatch -q` | `refactor: move dispatch to platform` |
| `src/zeroth/core/observability/` | `src/zeroth/platform/observability/` | `uv run pytest tests/observability -q` | `refactor: move observability to platform` |
| `src/zeroth/core/secrets/` | `src/zeroth/platform/secrets/` | `uv run pytest tests/secrets -q` | `refactor: move secrets to platform` |
| `src/zeroth/core/signing/` | `src/zeroth/platform/signing/` | `uv run pytest tests/signing -q` | `refactor: move signing to platform` |

- [ ] For each row, add a non-golden canonical boundary test and confirm the missing-module failure, move exactly that directory, rewrite all `src/ tests/ examples/ docs/` imports, and remove its exact architecture exceptions.
- [ ] Run the row's focused tests, `uv run pytest tests/architecture tests/contracts/test_refactor_contract_snapshots.py -q`, Ruff both destination and affected tests, and the frontend guard before the production commit. Then update the canonical fixture and migration row, rerun surface/snapshot tests, and commit that fixture/docs update separately.

### Task 12: Move contract and governed-contract packages

**Package slices:**

| Source | Destination | Focused tests | Commit |
| --- | --- | --- | --- |
| `src/zeroth/core/contracts/` | `src/zeroth/contracts/registry/` | `uv run pytest tests/contracts -q` | `refactor: move registry contracts` |
| `src/zeroth/core/conditions/` | `src/zeroth/contracts/conditions/` | `uv run pytest tests/conditions -q` | `refactor: move condition contracts` |
| `src/zeroth/core/mappings/` | `src/zeroth/contracts/mappings/` | `uv run pytest tests/mappings -q` | `refactor: move mappings contracts` |
| `src/zeroth/core/templates/` | `src/zeroth/contracts/templates/` | `uv run pytest tests/templates -q` | `refactor: move template contracts` |
| remaining `src/zeroth/core/graph/` | `src/zeroth/contracts/graph/` | `uv run pytest tests/graph -q` | `refactor: move graph contracts` |
| `src/zeroth/core/governed/app/`, `models/` | `src/zeroth/contracts/governed/` | create and run `uv run pytest tests/contracts/governed -q` | `refactor: move governed contracts` |

- [ ] Execute the explicit red canonical import → move/rewrite → focused pass → snapshots/architecture → Ruff → frontend guard → production commit cycle for each table row, then update its canonical fixture/migration row and commit that passing docs change separately.
- [ ] Create `docs/governed-capability-disposition.md` before moving governed contracts and inventory every `core.governed` symbol; do not classify runtime/audit/memory/tools implementations yet.

### Task 13: Move governance packages

**Package slices:**

| Source | Destination | Focused tests | Commit |
| --- | --- | --- | --- |
| `core/approvals` | `governance/approvals` | `uv run pytest tests/approvals tests/service/test_approval_api.py -q` | `refactor: move approvals governance` |
| `core/audit` and maintained `core/governed/audit` | `governance/audit` | `uv run pytest tests/audit -q` | `refactor: consolidate audit governance` |
| `core/identity` | `governance/identity` | `uv run pytest tests/service/test_auth_api.py tests/service/test_bearer_auth.py -q` | `refactor: move identity governance` |
| `core/policy` | `governance/policy` | `uv run pytest tests/policy -q` | `refactor: move policy governance` |
| `core/guardrails` | `governance/guardrails` | `uv run pytest tests/guardrails -q` | `refactor: move guardrails governance` |
| remaining `core/retention` | `governance/retention` | `uv run pytest tests/retention tests/governance/retention -q` | `refactor: move retention governance` |

- [ ] For any governed consolidation, add an equivalence test first and confirm it fails when pointed at the not-yet-existing canonical location; never delete a legacy implementation without a disposition row.
- [ ] Execute the same per-row red/move/focused-pass/snapshots/architecture/Ruff/frontend/commit cycle from Task 11.

### Task 14: Move remaining runtime and economics packages

**Package slices, in order:**

| Source | Destination | Focused tests | Commit |
| --- | --- | --- | --- |
| `core/context_window` | `runtime/context` | `uv run pytest tests/context_window -q` | `refactor: move runtime context` |
| remaining `core/parallel` | `runtime/parallel` | `uv run pytest tests/parallel -q` | `refactor: move parallel runtime` |
| `core/subgraph` | `runtime/subgraphs` | `uv run pytest tests/subgraph -q` | `refactor: move subgraph runtime` |
| remaining `core/agent_runtime` plus maintained `core/governed/runtime`/`tools` | `runtime/agents` and `runtime/orchestration` | create `tests/runtime/governed/`; run `uv run pytest tests/agent_runtime tests/orchestrator tests/runtime/governed -q` | `refactor: consolidate agent runtime` |
| `core/econ` | `econ/analytics` and `econ/instrumentation` | `uv run pytest tests/test_econ_adapter.py tests/test_econ_analytics_api.py tests/test_econ_budget.py tests/test_econ_models.py tests/test_econ_opportunities.py tests/test_econ_quality.py tests/test_econ_rightsizing.py tests/test_econ_rightsizing_experiment.py tests/test_econ_unit_economics.py tests/test_econ_waste.py tests/test_econ_waste_rollup.py -q` | `refactor: move economic analytics` |
| `zeroth/econ_plane` | `econ/plane` | `uv run pytest tests/test_regulus_mount.py tests/retention/test_econ_hook.py tests/service/test_admin_api.py tests/contracts/test_refactor_contract_snapshots.py -q` | `refactor: move economic control plane` |

- [ ] Snapshot exported economics and service routes before the economics rows.
- [ ] Execute the per-row red/move/focused-pass/snapshots/architecture/Ruff/frontend/commit cycle.
- [ ] Assert with `uv run pytest tests/architecture/test_backend_dependencies.py -v` that runtime has no import of `zeroth.integrations`.

### Task 15: Move integrations and evaluation

**Package slices:**

| Source | Destination | Focused tests | Commit |
| --- | --- | --- | --- |
| `core/http` | `integrations/http` | `uv run pytest tests/http -q` | `refactor: move http integration` |
| `core/rag` | `integrations/rag` | `uv run pytest tests/rag -q` | `refactor: move rag integration` |
| `core/execution_units` | `integrations/execution` | `uv run pytest tests/execution_units -q` | `refactor: move execution integrations` |
| `core/sandbox_sidecar` | `integrations/sandbox` | `uv run pytest tests/sandbox_sidecar tests/execution_units/test_sandbox.py tests/execution_units/test_sandbox_hardening.py tests/execution_units/test_sandbox_strict_network.py -q` | `refactor: move sandbox integrations` |
| `core/memory` plus maintained `core/governed/memory` | `integrations/memory` | `uv run pytest tests/memory -q` | `refactor: consolidate memory integrations` |
| `core/eval` | `eval` | `uv run pytest tests/eval -q` | `refactor: move evaluation library` |

- [ ] Run `uv sync --all-extras` before the first row.
- [ ] Execute the per-row red/move/focused-pass/snapshots/architecture/Ruff/frontend/commit cycle; optional integrations must remain importable in the all-extras environment.

### Task 16: Move deployment and webhook service domains

**Files:**
- Move: `src/zeroth/core/deployments/` → `src/zeroth/service/deployments/`
- Move: `src/zeroth/core/webhooks/` → `src/zeroth/service/webhooks/`
- Modify: service API/bootstrap imports and architecture exceptions in production commits; canonical fixture and migration guide only in separate follow-up commits

- [ ] Add failing canonical import/signature tests for deployment models/repository/service; move/rewrite, run `uv run pytest tests/deployments tests/service/test_deployment_api.py -q`, snapshots, Ruff, frontend guard, and commit `refactor: move deployment service domain`; then update fixture/guide, rerun surface/snapshots, and separately commit `docs: record deployment import migration`.
- [ ] Add failing canonical import/signature tests for webhook models/repository/service; move/rewrite, run `uv run pytest tests/test_webhook_api.py tests/test_webhook_delivery.py tests/test_webhook_event_emission.py tests/test_webhook_models.py tests/test_webhook_repository.py tests/test_webhook_service.py -q`, snapshots, Ruff, frontend guard, and commit `refactor: move webhook service domain`; then update fixture/guide, rerun surface/snapshots, and separately commit `docs: record webhook import migration`.

### Task 17: Dead-code and duplication audit

**Files:**
- Create: `docs/backend-dead-code-audit.md`
- Modify only individually proven dead/superseded source files and exports

- [ ] Refresh the graph with `build_or_update_graph(full_rebuild=true)` and generate `refactor_tool(mode=dead_code)` candidates.
- [ ] For each candidate, record exact `rg` searches across `src tests examples docs`, package exports, schemas, entry points, optional extras, replacement behavior, and tests.
- [ ] Add an equivalence test that passes against both the legacy and maintained implementations before deletion. If parity fails, implement parity in a separate behavior-preserving red-green task and commit; do not delete until equivalence passes.
- [ ] Delete only candidates satisfying every criterion; leave uncertain/library-useful symbols intact and record why.
- [ ] Run the candidate's focused tests, canonical/legacy fixtures, Ruff, architecture test, and frontend guard for every deletion commit.
- [ ] Commit small groups with explicit subjects such as `refactor: remove superseded <capability>`.

### Task 18: Final migration cleanup and verification

**Files:**
- Modify: `docs/backend-library-surface.md`
- Modify: `docs/backend-import-migration.md`
- Modify: architecture exception map; it must contain no undocumented temporary entries
- Modify: any stale backend imports in docs/examples/tests

- [ ] Run `uv sync --all-extras`.
- [ ] Run `uv run pytest tests/architecture tests/contracts -v`.
- [ ] Run `uv run pytest -v` and record the complete result.
- [ ] Run `uv run ruff check src/`.
- [ ] Run `uv run ruff format --check src/`.
- [ ] Run `git diff --exit-code 01b36a9 -- frontend/`.
- [ ] Refresh the code graph and run large-file, dead-code, affected-flow, and dependency audits.
- [ ] Confirm the inventory imports, signatures, OpenAPI snapshot, schema/migration snapshot, serialization round trips, and exception fixtures all pass.
- [ ] Commit documentation and final exception cleanup: `docs: complete backend architecture migration`.
- [ ] Report exact test totals, warnings, remaining large files, retained dead-code candidates, commits, and import migrations.

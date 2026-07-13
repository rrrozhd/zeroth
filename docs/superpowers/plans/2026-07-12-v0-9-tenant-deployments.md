# Tenant-Safe Deployment Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make graph and deployment ownership consistently tenant+workspace scoped across Studio, repositories, services, and APIs.

**Architecture:** `Graph.tenant_id` and `Graph.workspace_id` are the source of truth. Migration 009 adds a nullable graph workspace column; legacy NULL-workspace graphs remain visible only to NULL-workspace principals.

**Tech Stack:** Pydantic v2, FastAPI, Alembic, SQLite/Postgres, pytest.

---

### Task 1: Add workspace ownership to graphs and migration 009

**Files:**
- Create: `src/zeroth/core/migrations/versions/009_add_graph_workspace_scope.py`
- Modify: `src/zeroth/core/graph/models.py:499`
- Modify: `src/zeroth/core/graph/repository.py`
- Test: `tests/graph/test_graph_repository_tenant.py`
- Test: `tests/storage/test_migration_workspace_scope.py`

- [x] Write RED tests for saving/filtering `workspace_id`, legacy NULL visibility, and upgrade→downgrade→upgrade.
- [x] Run: `uv run pytest -q tests/graph/test_graph_repository_tenant.py tests/storage/test_migration_workspace_scope.py` and confirm failures for the missing field/column.
- [x] Add `workspace_id: str | None = None` to `Graph`.
- [x] Add the column with Alembic `op.add_column("graph_versions", sa.Column("workspace_id", sa.String(), nullable=True))` and an index on `(tenant_id, workspace_id)`; use `batch_alter_table` for downgrade.
- [x] Update graph insert/update/get/list helpers so exact scope uses `workspace_id IS NULL` for `None` and `workspace_id = ?` otherwise. Never treat NULL as a wildcard.
- [x] Run GREEN and commit as `feat: persist graph workspace ownership`.

### Task 2: Stamp Studio ownership and scope every workflow operation

**Files:**
- Modify: `src/zeroth/core/service/studio_api.py`
- Test: `tests/service/test_cross_tenant_leak_matrix.py`

- [x] Add RED API tests with two principals in the same tenant but different workspaces plus a NULL-workspace legacy principal.
- [x] Confirm workspace B receives 404 for workspace A create/read/update/publish/diff/clone/archive operations.
- [x] Stamp both principal fields on create and pass both to repository methods for every endpoint.
- [x] Replace pre-check-then-global calls (`publish`, `diff`, `clone`) with repository methods accepting exact scope, so the check and mutation cannot diverge.
- [x] Run GREEN and commit as `fix: enforce Studio workspace scope`.

### Task 3: Make deployment service scope explicit

**Files:**
- Modify: `src/zeroth/core/deployments/repository.py`
- Modify: `src/zeroth/core/deployments/service.py`
- Test: `tests/deployments/test_service.py`
- Test: `tests/service/test_cross_tenant_leak_matrix.py`

- [x] Write RED tests proving deployment tenant comes from `Graph.tenant_id`, not `deployment_settings`, and foreign scopes cannot deploy/rollback/supersede.
- [x] Extend repository `get/list/next_version/create` filters with exact tenant+workspace scope; keep deployment refs globally unique.
- [x] Extend `DeploymentService.deploy(..., tenant_id=None, workspace_id=None)` so supplied scope constrains graph load and lineage; always stamp the deployment from the graph owner.
- [x] Extend rollback with the same scope and ensure foreign-ref collisions return a not-found-style service error before mutation.
- [x] Run GREEN and commit as `fix: scope deployment lifecycle operations`.

### Task 4: Scope deployment APIs by principal

**Files:**
- Modify: `src/zeroth/core/service/deployment_api.py`
- Test: `tests/service/test_deployment_api.py`

- [x] Write RED tests for no-serving-deployment listing, same-tenant/different-workspace isolation, foreign create, and foreign rollback.
- [x] Capture the principal returned by `require_permission` and pass its tenant/workspace to list/deploy/rollback.
- [x] Remove serving-deployment-derived listing scope and the unfiltered `None` fallback.
- [x] Run GREEN: `uv run pytest -q tests/service/test_deployment_api.py tests/service/test_cross_tenant_leak_matrix.py`.
- [x] Commit as `fix: tenant-scope deployment APIs`.

### Task 5: Compatibility verification

- [x] Update helper graphs/tests to set graph owner fields directly instead of hiding tenant in `deployment_settings`.
- [x] Run: `uv run pytest -q tests/graph tests/deployments tests/service`.
- [x] Run migration round-trip on SQLite; run marked Postgres migration test when Docker is available.
- [x] Commit only necessary compatibility edits.


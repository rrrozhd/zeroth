# Retention Policy Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Give audit TTL, run TTL, defaults, legal holds, and erasure validation distinct and correct semantics.

**Architecture:** Audit TTL tombstones individual old audits. Run TTL fully erases only old terminal runs. Tenant policies are whole-policy overrides; explicit NULL means keep forever, while only a missing policy inherits the configured system default.

**Tech Stack:** Pydantic v2, FastAPI, SQLite/Postgres repositories, pytest-asyncio.

**Dependency:** Complete `2026-07-12-v0-9-database-coordination.md` first.

---

### Task 1: Validate TTL inputs

**Files:**
- Modify: `src/zeroth/core/retention/models.py`
- Modify: `src/zeroth/core/config/settings.py`
- Modify: `src/zeroth/core/service/retention_api.py`
- Test: `tests/retention/test_retention_ttl.py`
- Test: `tests/service/test_retention_api.py`

- [x] Add RED parametrized tests for `0`, `-1`, and negative defaults; expect Pydantic 422/model validation errors.
- [x] Change every configured/API/model TTL type to `int | None` and use
  `Field(default=None, ge=1)`; fractional values must fail validation rather than
  truncate. Keep worker polling as a positive float because it is an interval, not a
  persisted TTL.
- [x] Run GREEN and commit as `fix: reject invalid retention TTLs`.

### Task 2: Make configured defaults effective

**Files:**
- Modify: `src/zeroth/core/retention/policy_repository.py`
- Modify: `src/zeroth/core/service/bootstrap.py`
- Test: `tests/retention/test_retention_ttl.py`
- Test: `tests/service/test_bootstrap_retention.py`

- [x] Write RED tests proving a missing tenant policy inherits configured audit/run defaults, while an explicit tenant policy with `audit_ttl_seconds=None` keeps audits forever even when the system default is finite.
- [x] Pass a `RetentionPolicy` default into `RetentionPolicyRepository`; `resolve()` returns an owner-adjusted copy only when the tenant row is absent.
- [x] Bootstrap the repository default from `settings.retention.default_*` without overwriting an explicit tenant policy or persisting environment-derived defaults as tenant rows.
- [x] Run GREEN and commit as `fix: apply configured retention defaults`.

### Task 3: Add efficient audit-TTL tombstoning

**Files:**
- Modify: `src/zeroth/core/audit/repository.py`
- Modify: `src/zeroth/core/retention/erasure_service.py`
- Test: `tests/retention/test_retention_ttl.py`

- [x] Write RED test with one run containing old and new audits; after audit sweep only the old v2 audit is erased, while run/checkpoints/new audit remain intact.
- [x] Add a projection query returning erasable audit IDs by tenant/cutoff/held runs without deserializing all records.
- [x] Add `purge_audits(tenant_id, cutoff, holds)` that uses the tenant coordination transaction, re-checks holds, and tombstones only selected records.
- [x] Track `audits_erased` independently in worker results/logs.
- [x] Run GREEN and commit as `fix: separate audit TTL from run erasure`.

### Task 4: Add terminal-run TTL selection

**Files:**
- Modify: `src/zeroth/core/runs/repository.py`
- Modify: `src/zeroth/core/retention/erasure_service.py`
- Test: `tests/retention/test_retention_ttl.py`

- [x] Write RED tests covering old `COMPLETED`/`FAILED` runs, old `PENDING`,
  `RUNNING`, `WAITING_APPROVAL`, and `WAITING_INTERRUPT` runs, and a recently updated
  terminal run. Do not introduce a cancellation state; the current GovernAI enum has
  exactly these six statuses.
- [x] Add `list_erasable_run_ids(tenant_id, older_than, terminal_statuses={RunStatus.COMPLETED, RunStatus.FAILED})` selecting by persisted `updated_at`, tenant, and terminal status.
- [x] Add a connection-aware `lock_and_recheck_erasable_run(connection, run_id,
  tenant_id, cutoff)` that locks the run row on Postgres (and relies on the SQLite
  write transaction), then rechecks tenant, status, and `updated_at` inside the same
  destructive transaction. Return `None` when replay/resume/update made it ineligible.
- [x] Add `purge_runs()` that passes the cutoff into full `erase_run(...,
  reason="ttl", ttl_cutoff=cutoff)`. The erasure transaction must call the recheck
  before any audit/checkpoint/run mutation.
- [x] Add a barrier RED/GREEN test that selects a `FAILED` run, changes it to `PENDING`
  before erasure acquires the lock, and proves zero surfaces are erased.
- [x] Ensure a run with an old audit but recent `updated_at` is not fully erased.
- [x] Run GREEN and commit as `fix: enforce run TTL on terminal runs`.

### Task 5: Update worker orchestration and API semantics

**Files:**
- Modify: `src/zeroth/core/retention/worker.py`
- Modify: `docs/retention-and-erasure.md`
- Test: `tests/retention/test_worker.py`

- [x] Write RED worker test expecting both surfaces to run independently when configured.
- [x] Replace the single `purge_tenant` path with `purge_audits` and `purge_runs`, preserving per-tenant error isolation.
- [x] Document exact cutoffs, terminal statuses, NULL semantics, and legal-hold behavior.
- [x] Run GREEN and commit as `fix: run independent retention sweeps`.

### Task 6: Retention verification

- [x] Run `uv run pytest -q tests/retention tests/service/test_retention_api.py`.
- [x] Add query-count assertions using an instrumented connection: audit selection is one projection query plus batched updates, not one list-by-run query per record.
- [x] Run full audit verification after tombstoning to prove signed chains remain valid.
- [x] Run `uv run ruff check src/zeroth/core/retention src/zeroth/core/audit src/zeroth/core/runs`.

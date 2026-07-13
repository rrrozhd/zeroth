# Database Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee one continuous audit chain and race-free retention authorization across processes and database backends.

**Architecture:** Extend the storage protocol with explicit write-lock transactions and backend identity. A small coordination module owns row creation/locking; audit and retention repositories consume it without duplicating dialect logic.

**Tech Stack:** aiosqlite, psycopg3, Alembic, asyncio, pytest/testcontainers.

---

### Task 1: Define write-lock transaction semantics

**Files:**
- Modify: `src/zeroth/core/storage/database.py`
- Modify: `src/zeroth/core/storage/async_sqlite.py`
- Modify: `src/zeroth/core/storage/async_postgres.py`
- Test: `tests/storage/test_coordination.py`

- [ ] Write RED tests that open two SQLite database instances against one file, hold `transaction(write_lock=True)` in the first, and prove the second cannot enter its critical section until release.
- [ ] Add `backend: Literal["sqlite", "postgres"]` and `transaction(*, write_lock: bool = False)` to `AsyncDatabase`.
- [ ] SQLite: execute `BEGIN IMMEDIATE` before yielding when `write_lock=True`.
  Ordinary transactions deliberately retain aiosqlite's lazy transaction start:
  explicit deferred `BEGIN` creates stale WAL read snapshots in existing
  read-then-write repository flows and causes non-retryable `SQLITE_BUSY_SNAPSHOT`.
  Configure a bounded busy timeout and raise `CoordinationTimeoutError` on lock timeout.
- [ ] Postgres: expose `backend = "postgres"` and, for `write_lock=True`, execute
  `SET LOCAL lock_timeout = '<configured milliseconds>ms'` before yielding. Map
  psycopg lock-timeout/query-cancel exceptions to the same `CoordinationTimeoutError`.
  Add `coordination_timeout_seconds` to both database constructors with one bounded
  default used by SQLite busy timeout and Postgres `lock_timeout`.
- [ ] Run GREEN: `uv run pytest -q tests/storage/test_coordination.py tests/storage/test_sqlite.py`.
- [ ] Commit as `feat: add database coordination transactions`.

### Task 2: Add migration 010 and row-lock helper

**Files:**
- Create: `src/zeroth/core/migrations/versions/010_add_coordination_rows.py`
- Create: `src/zeroth/core/storage/coordination.py`
- Modify: `src/zeroth/core/storage/__init__.py`
- Test: `tests/storage/test_coordination.py`
- Test: `tests/storage/test_migration_coordination.py`

- [ ] Write RED migration tests for `audit_chain_heads(run_id PK, head_digest,
  next_sequence, updated_at)`, a nullable `node_audits.chain_sequence` column plus
  unique `(run_id, chain_sequence)` index, and
  `retention_coordination(tenant_id PK, updated_at)`, including downgrade/upgrade
  round-trip. Backfill existing audit sequences per run using stored
  `created_at, audit_id` order; legacy records retain compatibility.
- [ ] Implement `ensure_and_lock_row(connection, *, backend, table, key_column, key)`:

```python
await connection.execute(
    f"INSERT INTO {table} ({key_column}, updated_at) VALUES (?, ?) "
    f"ON CONFLICT({key_column}) DO NOTHING",
    (key, datetime.now(UTC).isoformat()),
)
suffix = " FOR UPDATE" if backend == "postgres" else ""
return await connection.fetch_one(
    f"SELECT * FROM {table} WHERE {key_column} = ?{suffix}", (key,)
)
```

Use a fixed allow-list of table/key pairs; do not accept arbitrary caller-controlled SQL identifiers.
- [ ] Add a Postgres contention test in which one transaction holds a coordination
  row and a second `write_lock=True` transaction fails with
  `CoordinationTimeoutError` within the configured bound; never allow an indefinite
  `SELECT ... FOR UPDATE` wait.
- [ ] Run the SQLite migration/locking tests and the Postgres contention test when
  Docker is available, then commit as `feat: add audit and retention coordination rows`.

### Task 3: Make audit head advancement transactional

**Files:**
- Create: `src/zeroth/core/audit/coordination.py`
- Modify: `src/zeroth/core/audit/models.py`
- Modify: `src/zeroth/core/audit/repository.py`
- Modify: `src/zeroth/core/audit/verifier.py`
- Test: `tests/audit/test_audit_concurrency.py`

- [ ] Write RED test using two `AuditRepository` instances and `asyncio.gather` to
  append 20 records to one run with deliberately equal/reversed application
  timestamps; verify every write succeeds, persisted `chain_sequence` is exactly
  `1..20`, and `AuditContinuityVerifier.verify_run()` reports one 20-record chain.
- [ ] Add `NodeAuditRecord.chain_sequence: int | None = None` and include it in the
  digest-excluded compatibility fields. Repository hydration selects the dedicated
  column and injects it into the model; legacy missing/NULL values continue to work.
- [ ] In a `transaction(write_lock=True)`, lock/initialize the run head, lazily derive
  it and the next sequence from existing rows when the coordination row is empty,
  allocate `chain_sequence = next_sequence`, compute the chained record, insert both
  JSON and dedicated sequence column, then advance digest plus sequence before commit.
- [ ] Order per-run audit reads and verification by `chain_sequence`, falling back to
  `created_at, audit_id` only for legacy NULL-sequence rows. Deployment verification
  explicitly sorts each grouped run by this rule rather than trusting cross-run query
  order.
- [ ] Keep the local `asyncio.Lock` only as a same-process contention optimization; instantiate it per repository only if tests prove no correctness dependency.
- [ ] Add failure-injection test: duplicate audit ID rolls back head advancement and the next valid write chains from the prior head.
- [ ] Run GREEN: `uv run pytest -q tests/audit/test_audit_concurrency.py tests/audit`.
- [ ] Add a marked Postgres version using two pooled connections; run when Docker is available.
- [ ] Commit as `fix: coordinate audit chains across workers`.

### Task 4: Add tenant-level retention lock API

**Files:**
- Create: `src/zeroth/core/retention/coordination.py`
- Modify: `src/zeroth/core/retention/legal_hold_repository.py`
- Test: `tests/retention/test_coordination.py`

- [ ] Write RED tests proving run-specific and tenant-wide hold operations contend on the same tenant row.
- [ ] Implement `RetentionCoordinator.transaction(tenant_id)` using `database.transaction(write_lock=True)` and `ensure_and_lock_row(... retention_coordination ...)`.
- [ ] Add connection-aware `place_in_transaction`, `release_in_transaction`, and `active_holds_for_tenant_in_transaction`; public methods open the coordinator transaction.
- [ ] Do not add a second run-level lock.
- [ ] Run GREEN and commit as `feat: serialize tenant retention administration`.

### Task 5: Integrate atomic database erasure authorization

**Files:**
- Modify: `src/zeroth/core/retention/erasure_service.py`
- Modify: `src/zeroth/core/audit/repository.py`
- Modify: `src/zeroth/core/runs/repository.py`
- Modify: `src/zeroth/core/retention/audit_log_repository.py`
- Test: `tests/retention/test_coordination.py`

- [ ] Write a barrier-based RED test: pause erasure after lock acquisition, attempt hold placement from a second service/repository instance, then prove deterministic serialization and no stale hold check.
- [ ] Add connection-aware audit tombstone, checkpoint delete, run redact, and
  `RetentionAuditLogRepository.record_in_transaction(connection, ...)` methods. The
  public `record()` delegates to the connection-aware method inside its own transaction.
- [ ] Under the tenant coordinator transaction: re-read holds, reject if blocked,
  harvest database-resident artifact keys and economic join keys, tombstone/redact/
  delete database surfaces, and write the `erasure_authorized` database-phase log
  through `record_in_transaction`. Its detail must persist the complete external
  cleanup manifest (`artifact_keys`, `join_keys`, and cleanup status), so retries never
  depend on plaintext that has already been erased. Do not call any repository method
  that opens a nested transaction while the SQLite write lock is held.
- [ ] After commit: perform artifact/econ cleanup and append completion/failure log
  entries idempotently. Add `retry_external_cleanup(log_id)` that reloads the persisted
  manifest and skips items already marked complete.
- [ ] Add a RED/GREEN failure-injection test: external cleanup fails after database
  commit, a retry reconstructs all keys from the authorization log, and the second
  attempt completes without double-counting successful deletions.
- [ ] Run GREEN: `uv run pytest -q tests/retention/test_coordination.py tests/retention/test_erasure_service.py`.
- [ ] Commit as `fix: make legal holds and erasure race-free`.

### Task 6: Coordination verification

- [ ] Run `uv run pytest -q tests/storage tests/audit tests/retention`.
- [ ] Run Postgres marked tests with `uv run pytest -q -m postgres tests/storage/test_coordination.py tests/audit/test_audit_concurrency.py` when Docker is available.
- [ ] Run `uv run alembic upgrade 010`, downgrade to 008, then upgrade to head on disposable SQLite and Postgres databases.
- [ ] Record any unavailable Postgres verification explicitly; do not silently claim it passed.

# Structured Token Lifecycle Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the durable structured-token lifecycle for pause, graceful stop, cancellation fencing and settlement, retry/replay recovery, and `SubgraphNode` execution without changing the explicit legacy-OFF path.

**Architecture:** Add a focused `token_lifecycle.py` adapter that owns pure snapshot lifecycle transitions plus CAS persistence. `InterruptManager` exposes durable pause/resume/cancel commands through that adapter, `RunWorker` uses it when stopping or recovering token runs, and subgraph execution gains a runtime-neutral dispatch seam that the token coordinator can call through one isolated hook. Existing scheduler transitions and `token_runtime.py` remain the execution engine; lifecycle policy stays out of the loop coordinator.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2 immutable token contracts, repository CAS, pytest, Ruff, uv.

---

### Task 1: Durable lifecycle adapter

**Files:**
- Create: `src/zeroth/runtime/orchestration/token_lifecycle.py`
- Test: `tests/runtime/orchestration/test_token_lifecycle.py`

- [ ] Write failing tests for pause/resume identity, graceful stopped snapshots, cancellation generation increments, queued/join-waiting settlement, executing cancellation requests, nested fork/loop obligation settlement, acknowledgements, terminal compaction, and stale completion rejection.
- [ ] Run `uv run pytest -q tests/runtime/orchestration/test_token_lifecycle.py`; expect failures because the adapter does not exist.
- [ ] Implement immutable transitions and a CAS-retrying `TokenLifecycleAdapter` over `TokenSnapshotStore`.
- [ ] Run the focused lifecycle tests and `tests/contracts/graph/test_token_snapshot.py tests/runtime/orchestration/test_token_scheduler.py`; expect all pass.
- [ ] Run Ruff on the touched files and commit with a normal atomic commit.

### Task 2: Interrupt and worker lifecycle routing

**Files:**
- Modify: `src/zeroth/runtime/orchestration/interrupts.py`
- Modify: `src/zeroth/runtime/orchestration/run_worker.py`
- Test: `tests/runtime/orchestration/test_interrupts.py`
- Test: `tests/dispatch/test_worker.py`
- Test: `tests/dispatch/test_recovery.py`

- [ ] Write failing tests proving pause/cancel requests persist token snapshots, timed-out graceful shutdown records `STOPPED` rather than erasing replay state, recovery resumes stopped snapshots, and ordinary in-flight dispatch recovery increments attempts while preserving dispatch/idempotency identity.
- [ ] Run the new focused tests and observe the expected failures.
- [ ] Add optional lifecycle-adapter dependencies and route only token-enabled runs through them; preserve all existing behavior when no token snapshot exists.
- [ ] Run focused interrupt/worker/recovery tests, Ruff, and commit atomically.

### Task 3: SubgraphNode structured-token routing

**Files:**
- Modify: `src/zeroth/runtime/subgraphs/executor.py`
- Modify: `src/zeroth/runtime/orchestration/dispatcher.py`
- Modify: `src/zeroth/runtime/orchestration/token_runtime.py` (one isolated coordinator hook only)
- Test: `tests/runtime/test_subgraph_surface.py`
- Test: `tests/runtime/orchestration/test_dispatcher.py`
- Test: `tests/orchestrator/test_token_runtime_adapter.py`

- [ ] Write failing tests for first-run and resumed `SubgraphNode` dispatch through the structured-token runtime, including child pause/failure propagation and audit payloads.
- [ ] Run the focused tests and observe the existing unsupported-node failure.
- [ ] Add a subgraph dispatch seam that returns normal output/audit data and call it from the coordinator through a minimal isolated branch.
- [ ] Verify legacy sequential dispatch and explicit `sequential_join_enabled=False` remain unchanged.
- [ ] Run focused subgraph/runtime tests, Ruff, and commit the coordinator hook separately if it is unavoidable; document its merge-conflict surface.

### Task 4: Replay, integration, and final verification

**Files:**
- Modify only tests needed to close integration gaps discovered by the focused runs.

- [ ] Add replay tests for paused, stopped, and cancelled snapshots plus stale completions after reload.
- [ ] Run focused lifecycle/replay/subgraph tests.
- [ ] Run `uv run pytest -q tests/orchestrator`.
- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`.
- [ ] Update the code-review graph, inspect `detect_changes`, affected flows, impact radius, and tests-for coverage.
- [ ] Commit any final coherent test/documentation slice normally and report every commit SHA plus integration notes.

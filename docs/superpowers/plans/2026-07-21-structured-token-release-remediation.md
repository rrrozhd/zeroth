# Structured Token Release Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the lifecycle, deployment-pinning, and checker lines onto the durable loop runtime, remediate every reviewed structured-token release blocker, and run all executable release gates without enabling the default.

**Architecture:** Preserve the existing immutable snapshot/CAS boundary and reconcile branch work semantically at the coordinator and loop transition seams. Keep loop grammar, lifecycle policy, deployment hydration, and checker/oracle logic in focused modules; production checking must exercise the same repository-backed joins, loops, lifecycle transitions, and persisted snapshots as runtime execution. Add regression tests before every behavior change and commit each integration or fix independently.

**Tech Stack:** Python 3.12, Pydantic v2 immutable contracts, asyncio, repository CAS, pytest, Ruff, uv.

---

### Task 1: Preserve context and integrate lifecycle

**Files:**
- Preserve: `.planning/b9-token-engine-overlay-2026-07-20.md`
- Integrate: `src/zeroth/runtime/orchestration/token_lifecycle.py`
- Resolve: `src/zeroth/runtime/orchestration/token_runtime.py`
- Resolve: `tests/orchestrator/test_token_runtime_adapter.py`
- Test: `tests/orchestrator/test_token_runtime_lifecycle.py`
- Test: `tests/runtime/orchestration/test_token_lifecycle.py`

- [ ] Confirm durable branches retain deployment tip `70f9e37`, checker tip `572558c`, and lifecycle tip `8b71e62`.
- [ ] Cherry-pick lifecycle commits in original order and stop at each conflict.
- [ ] Resolve `token_runtime.py` by retaining loop entry/settlement/exit behavior while adding lifecycle fencing, recovery, subgraph dispatch, and terminal settlement.
- [ ] Resolve adapter tests by retaining both loop-runtime and lifecycle/subgraph cases.
- [ ] Run lifecycle, loop, dispatcher, worker, interrupt, admin, and adapter tests.
- [ ] Run the overlay cold-import, lazy-attribute, architecture-boundary, library-surface, and dependency checks before the next integration phase.
- [ ] Commit only semantic conflict resolution if it is not already represented by the cherry-pick commits.

### Task 2: Integrate immutable deployment pinning

**Files:**
- Modify: `src/zeroth/contracts/graph/models.py`
- Modify: `src/zeroth/contracts/graph/serialization.py`
- Modify: `src/zeroth/core/deployments/models.py`
- Create: `src/zeroth/core/migrations/versions/015_pin_deployment_engine_mode.py`
- Modify: `src/zeroth/runtime/subgraphs/resolver.py`
- Modify: `src/zeroth/service/deployments/{models,provenance,repository,service}.py`
- Test: `tests/deployments/test_{service,hydration,attestation_signing}.py`
- Test: `tests/storage/test_migration_deployment_engine_mode.py`
- Test: `tests/graph/test_models.py`

- [ ] Cherry-pick the three deployment commits in order and resolve shared loop files without changing runtime semantics.
- [ ] Verify absent/explicit-true/explicit-false authored settings round-trip distinctly.
- [ ] Verify immutable publication pins effective engine mode and shared hydration honors the pin for top-level and subgraph resolution.
- [ ] Verify v1 attestations remain valid and the migration backfills historical deployments deterministically.
- [ ] Verify the settings/runtime schema version is persisted and migrated without collapsing absent/true/false authored state.
- [ ] Run deployment, serialization, migration, contract, and service tests, then commit any semantic integration fix.
- [ ] Run the overlay cold-import, lazy-attribute, architecture-boundary, library-surface, and dependency checks before checker integration.

### Task 3: Integrate checker baseline

**Files:**
- Create: `scripts/check_token_engine.py`
- Create: `scripts/token_engine_checker/*.py`
- Create: `tests/scripts/test_token_engine_{grammar,oracle,adapter,checker}.py`

- [ ] Cherry-pick the checker commits in original order.
- [ ] Run the focused checker tests and record failures as the baseline for the reviewed defect classes.
- [ ] Resolve only mechanical integration conflicts; defer semantic checker remediation to Tasks 6–8.
- [ ] Run the overlay cold-import, lazy-attribute, architecture-boundary, library-surface, and dependency checks before semantic remediation.

### Task 4: Accept every valid structured loop delivery shape

**Files:**
- Modify: `src/zeroth/runtime/orchestration/token_loop_{entry,forks,settlement,closure,helpers}.py`
- Modify: `src/zeroth/runtime/orchestration/token_runtime.py`
- Test: `tests/runtime/orchestration/test_token_loops.py`
- Test: `tests/orchestrator/test_token_runtime_adapter.py`

- [ ] Add focused tests for several active boundary deliveries: same exit, different exits, back-edge plus exit, and nested-loop ownership.
- [ ] Include unique payload fingerprints, deterministic ordering, repository reload/replay, and exact durable child outcomes.
- [ ] Run each new test and confirm it fails because the current mixed header-body/exit rejection rejects a valid shape.
- [ ] Trace the rejection to its owning transition and compare with existing single-boundary and nested-loop cases.
- [ ] Remove only the invalid shape guard and materialize one durable child outcome per active delivery.
- [ ] Run focused loop/replay tests, then wider runtime orchestration tests and commit.

### Task 5: Correct cancellation and graceful-stop lifecycle semantics

**Files:**
- Modify: `src/zeroth/runtime/orchestration/token_lifecycle.py`
- Modify: `src/zeroth/runtime/orchestration/token_runtime.py`
- Modify: `src/zeroth/runtime/orchestration/interrupts.py`
- Modify: `src/zeroth/runtime/orchestration/run_worker.py`
- Test: `tests/runtime/orchestration/test_token_lifecycle.py`
- Test: `tests/orchestrator/test_token_runtime_lifecycle.py`
- Test: `tests/runtime/orchestration/test_interrupts.py`

- [ ] Add failing tests for fail-fast and best-effort cancellation, nested inner-first settlement, exactly one propagated parent outcome, and replay after each boundary.
- [ ] Add failing tests proving graceful stop blocks new top-level work but allows already-owned fork/join/loop continuations to drain; preserve pause as a dispatch freeze.
- [ ] Add failing tests for partial duplicate cancellation acknowledgement where fences store token IDs, CAS races, retries after reload, and stale dispatch completions.
- [ ] Add failing tests proving cancel prevents new child creation, settles queued children cancelled, and does not become terminal-cancelled until each executing child acknowledges or is durably fenced.
- [ ] Add failing invariant tests proving stopped/cancelled snapshots contain no orphan tokens and malformed, contradictory, or orphaned persisted state fails loudly.
- [ ] Trace policy, ownership, fence identity, and generation data through lifecycle and coordinator transitions before changing code.
- [ ] Implement minimal policy-aware settlement and token-ID-based acknowledgement idempotency.
- [ ] Run focused lifecycle/replay/race tests and orchestration tests, then commit.

### Task 6: Make grammar-v1 topology enumeration truthful

**Files:**
- Modify: `scripts/token_engine_checker/generator.py`
- Modify: `scripts/token_engine_checker/models.py`
- Modify: `scripts/token_engine_checker/oracle.py`
- Test: `tests/scripts/test_token_engine_grammar.py`
- Test: `tests/scripts/test_token_engine_oracle.py`

- [ ] Add failing cardinality and membership tests for all labelled N=4 valid topologies, including non-Hamiltonian diamonds, enabled masks, and condition valuations.
- [ ] Add a round-trip assertion that every generated case is classified valid and every valid enumerated case is generated.
- [ ] Replace path-seeded enumeration with complete labelled-edge enumeration plus grammar-v1 reducibility/safety filtering.
- [ ] Run focused grammar/oracle tests and a small exhaustive checker smoke test, then commit.

### Task 7: Make scheduling and the production adapter real

**Files:**
- Modify: `scripts/token_engine_checker/explorer.py`
- Modify: `scripts/token_engine_checker/adapter.py`
- Modify: `scripts/token_engine_checker/normalization.py`
- Test: `tests/scripts/test_token_engine_adapter.py`
- Test: `tests/scripts/test_token_engine_checker.py`

- [ ] Add failing tests proving schedules contain actual ready token IDs/state choices and materially alter dispatch order.
- [ ] Add failing coverage tests: ready width at most six explores every permutation; wider states explore canonical, reverse, and seeded schedules.
- [ ] Add failing production-trace tests for repository CAS/reload plus join, fork, loop, cancellation, retry, failure policy, checkpoint/replay, terminal output, and persisted state.
- [ ] Cover pause; disabled forward/back/exit edges; none/one/several active condition outcomes; nested-loop entry/exit; and `collect`, `merge`, `reduce`, and deterministic custom reducers.
- [ ] For every matrix cell, compare uninterrupted execution with repository-reloaded replay, or document the exact existing test that already proves the cell before adding new code.
- [ ] Replace queue-only adapter behavior with production coordinator/transition calls against a repository-backed snapshot store.
- [ ] Expand normalized traces so every required lifecycle and persisted-state field affects equivalence while only contract-declared nondeterminism is ignored.
- [ ] Run adapter/checker tests and representative checker cases, then commit.

### Task 8: Make mutations and coverage accounting release-grade

**Files:**
- Modify: `scripts/token_engine_checker/mutations.py`
- Modify: `scripts/token_engine_checker/runner.py`
- Modify: `scripts/token_engine_checker/reporting.py`
- Modify: `scripts/token_engine_checker/shrinker.py`
- Test: `tests/scripts/test_token_engine_checker.py`

- [ ] Add a seeded mutation per known defect class and a failing meta-test proving the clean oracle kills each mutation.
- [ ] Make unsupported valid cases hard failures and preserve minimized counterexamples.
- [ ] Add failing accounting tests separating logical eligible/executed topology, state, exhaustive-schedule, and sampled-schedule counts from cached transition invocations.
- [ ] Implement non-inflating counters and machine-readable report fields for Git SHA, grammar version, seeds, mutations, failures, and coverage dimensions.
- [ ] Run checker tests plus N=4 smoke/exhaustive checks as feasible, then commit.

### Task 9: Register explicit legacy-OFF compatibility coverage

**Files:**
- Modify: `pyproject.toml`
- Modify/Test: `tests/orchestrator/test_token_runtime_adapter.py`
- Create or modify: `tests/orchestrator/test_legacy_engine_compatibility.py`

- [ ] Add tests explicitly setting `sequential_join_enabled=False` for representative sequential, fork/join, loop, cancellation, and replay behavior.
- [ ] Run the marker command and confirm it initially selects zero tests or lacks registration.
- [ ] Register `legacy_engine`, mark the explicit compatibility tests, and run `uv run pytest -q tests/orchestrator -m legacy_engine`.
- [ ] Keep legacy-OFF deprecation warnings deferred until the v0.12.x default/version flip; report this gate as intentionally not executed in this remediation branch.
- [ ] Run unmarked orchestrator tests and commit.

### Task 10: Final release-gate verification and independent review

**Files:**
- Modify only tests or implementation required by a reproduced gate failure.
- Do not modify frontend paths.
- Do not change the default or release version.

- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run pytest -q tests/orchestrator -m legacy_engine`.
- [ ] Run checker gates for N=4 exhaustive, N=5/10,000 seed 120500, and N=6/10,000 seed 120600; record exact counts and runtimes.
- [ ] Run `uv run ruff check src/ tests/` and formatting checks.
- [ ] Run overlay cold-import, lazy-attribute, architecture-boundary, library-surface, and dependency checks.
- [ ] Update the code-review graph; inspect change risk, affected flows, impact radius, and test coverage.
- [ ] Dispatch an independent adversarial reviewer on the candidate release SHA without implementation history.
- [ ] For every HIGH finding: reproduce with a failing test, fix and commit, rerun the complete affected and full release gates, then request a fresh independent review of the new SHA; repeat until the reviewed SHA has zero unresolved HIGH findings.
- [ ] If executable gates pass but review is still pending, stop before the default flip and report that review as the sole remaining gate.
- [ ] Report implicit-default-ON suite, v0.12.x deprecation-warning, default-on documentation, and release-metadata gates as deferred/not passed because this branch intentionally does not flip the default or version.
- [ ] Report all commit SHAs, exact counts/runtimes, gaps, and worktree cleanliness.

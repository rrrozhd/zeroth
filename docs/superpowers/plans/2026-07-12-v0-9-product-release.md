# Product Truth and v0.9.1 Release Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align product claims and release metadata with implemented behavior, restore formatting, add focused frontend tests, and document the deferred decomposition.

**Architecture:** Documentation follows behavior after plans 1–5. Formatting is isolated as a mechanical commit. Frontend logic is extracted only where needed for testability; broad UI decomposition remains documented, not implemented.

**Tech Stack:** Markdown, TOML, uv/hatchling, Ruff, Next.js 16, TypeScript, Vitest.

**Dependency:** Execute after behavioral plans 1–5 so wording reflects final behavior.

---

### Task 1: Correct README and product statement

**Files:**
- Modify: `README.md`
- Modify: `.planning/PROJECT.md`
- Modify: `SECURITY.md`
- Test: `tests/docs/test_product_claims.py`

- [ ] Write RED documentation assertions checking that README states restart/reload after deployment creation, Regulus-extra requirements, fail-open bare-install behavior, and current Next.js/embedded-econ-plane architecture.
- [ ] Update `.planning/PROJECT.md` from stale Vue/v4.1/280-test language to package v0.9, Next.js, embedded econ plane, and current known hardening status. Add one paragraph explaining historical roadmap `v4.x` versus package `0.x` versions.
- [ ] Update security notes for cross-worker audit coordination, Vault async behavior, and MCP process capabilities.
- [ ] Run GREEN and commit as `docs: align product claims with v0.9.1`.

### Task 2: Add changelog and bump patch version

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/docs/test_release_metadata.py`

- [ ] Write RED test asserting project and lock versions are `0.9.1` and changelog contains `[0.9.1]` with the six hardening workstreams.
- [ ] Add concise historical entries for releases omitted since 0.2, or explicitly group them under a documented pre-0.9 development section; do not fabricate dates/content not supported by git history.
- [ ] Change `project.version` to `0.9.1` and run `uv lock` to sync the editable package record.
- [ ] Do not tag, push, or publish.
- [ ] Run GREEN and commit as `chore: prepare v0.9.1 release metadata`.

### Task 3: Add focused frontend test infrastructure

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/app/studio/edit/runEligibility.ts`
- Create: `frontend/app/studio/edit/runEligibility.test.ts`
- Modify: `frontend/app/studio/edit/page.tsx:1583`

- [ ] Write the test file first against the wished-for pure API:

```ts
expect(servedGraphId("graph-a@3")).toBe("graph-a");
expect(canRunWorkflow("graph-a", "graph-a@3")).toBe(true);
expect(canRunWorkflow("graph-b", "graph-a@3")).toBe(false);
expect(canDeployWorkflow("published")).toBe(true);
expect(canDeployWorkflow("draft")).toBe(false);
expect(canDeployWorkflow("archived")).toBe(false);
```

- [ ] Run RED: `npm --prefix frontend test -- --run`; expected failure because Vitest/helper do not exist.
- [ ] Add Vitest dev dependency/config and implement the pure run-reference and
  published-status deployment eligibility helpers.
- [ ] Replace inline split/comparison in `RunPanel` and inline deployment-button
  eligibility in the editor toolbar with the helpers without changing UI behavior.
- [ ] Run GREEN plus `npm --prefix frontend run build`.
- [ ] Commit as `test: add Studio run eligibility coverage`.

### Task 4: Eliminate the unawaited-coroutine warning

**Files:**
- Modify: `tests/dispatch/test_worker.py`
- Test: `tests/dispatch/test_worker.py`

- [ ] Run the single test with `-W error::RuntimeWarning` and confirm RED.
- [ ] Change the test fixture/helper so it does not construct an unawaited coroutine merely to inspect a task name; use a scheduled task that is awaited/cancelled in `finally`, or a non-coroutine object if the production helper accepts it.
- [ ] Run: `uv run pytest -q -W error::RuntimeWarning tests/dispatch/test_worker.py::test_extract_run_id_from_task_name`.
- [ ] Commit as `test: close dispatch coroutine fixture`.

### Task 5: Apply formatting as a mechanical commit

**Files:**
- Modify: files selected by `uv run ruff format src/`

- [ ] Confirm plans 1–5 tests are green before formatting.
- [ ] Run `uv run ruff format src/`.
- [ ] Run `uv run ruff format --check src/` and `uv run ruff check src/`.
- [ ] Review `git diff --stat` and ensure only mechanical formatting occurred.
- [ ] Commit as `style: format Python sources`.

### Task 6: Document deferred runtime/Studio decomposition

**Files:**
- Create: `docs/architecture/runtime-studio-decomposition.md`

- [ ] Document current hotspots with measured line counts and dependency graph references.
- [ ] Define runtime seams: dispatch preparation, agent context, audit emission, parallel/subgraph coordination.
- [ ] Define Studio seams: editor state/controller, canvas, deployment modal, run panel, node inspector.
- [ ] Provide phased characterization-test gates, rollback points, non-goals, and target file-size/ownership boundaries.
- [ ] Commit as `docs: plan runtime and Studio decomposition`.

### Task 7: Full release-candidate verification

- [ ] Run `uv run pytest -q` and confirm no unawaited-coroutine warning.
- [ ] Run `uv run ruff check src/` and `uv run ruff format --check src/`.
- [ ] Run `npm --prefix frontend test -- --run` and `npm --prefix frontend run build`.
- [ ] Run `uv build --wheel`.
- [ ] Create a temporary clean virtualenv, install `dist/zeroth_core-0.9.1-py3-none-any.whl`, then run `python -c "import zeroth.core"` and `zeroth-core --help`.
- [ ] Run `git diff --check` and inspect `git status --short`.
- [ ] Confirm no tag, push, publication, or unrelated-user-file commit occurred.

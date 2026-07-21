# Console Semantic Port and Main Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port GitHub PR #4 onto the refactored Zeroth core, enable the structured-token engine by default, verify the integrated v0.12.0 release, and replace `origin/main` through a recoverable force-with-lease cutover.

**Architecture:** Treat PR #4 as a behavioral source rather than mergeable history. Transplant its console files, rebuild its Regulus proxy in `zeroth.service`, regenerate both OpenAPI clients from canonical new-core applications, and preserve destination-only behavior. Remote mutation is the final phase and uses immutable SHAs, verified backup refs, exact leases, and post-push CI gates.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, httpx, Next.js 16, React 19, TypeScript, Vitest, openapi-typescript, uv, Ruff, Git/GitHub CLI.

---

### Task 1: Freeze source evidence and preservation refs

**Files:**
- Create: `.planning/console-rebuild/SOURCE-INVENTORY.md`
- Verify: `docs/superpowers/specs/2026-07-21-console-semantic-port-design.md`

- [ ] **Step 1: Fetch and verify immutable inputs**

Run:

```bash
git fetch origin main feat/console-rebuild feat/custom-roles-and-approval-notifiers
git rev-parse origin/main
git rev-parse origin/feat/console-rebuild
gh pr view 4 --json state,headRefName,headRefOid,files,commits
```

Expected: PR #4 is open and its head is exactly
`4d39d15abab3608322ca20ee55a99f95c46607c5`. Stop if it differs.

- [ ] **Step 2: Create durable local preservation branches**

Create `codex/pre-refactor-main-20260721` at the freshly fetched main SHA and
`codex/preserve-console-rebuild` at the verified PR head. If either name exists
at a different SHA, stop rather than overwrite it.

- [ ] **Step 3: Capture the PR behavior inventory**

Write `SOURCE-INVENTORY.md` from the PR file list and source routes. Record all
nav destinations, pages, loading/empty/error states, polling hooks, optimistic
mutations, Studio interactions, generated schemas, dependencies, and the
destination-only `/webhooks` route that must remain.

- [ ] **Step 4: Verify and commit**

Run `git diff --check`, inspect the inventory against
`git diff d574ca8..4d39d15 -- frontend`, then commit:

```bash
git commit -m "docs: inventory console semantic port"
```

### Task 2: Establish failing console parity tests

**Files:**
- Create: `frontend/app/lib/navigation.test.ts`
- Create: `frontend/app/lib/version.test.ts`
- Create: `frontend/app/lib/regulus.test.ts`
- Create: `frontend/app/semantic-port/source-parity-cases.ts`
- Create: `frontend/app/semantic-port/routes-and-states.test.tsx`
- Create: `frontend/app/semantic-port/polling-and-mutations.test.tsx`
- Create: `frontend/app/semantic-port/studio-behavior.test.tsx`
- Create: `scripts/generate_frontend_version.py`
- Create: `tests/scripts/test_frontend_version.py`
- Modify: `frontend/vitest.config.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`

- [ ] **Step 1: Write failing navigation and version tests**

Test that the exported navigation inventory contains Operate, Build, Govern,
Regulus, Learn, and `/webhooks`; test that the displayed version comes from a
generated metadata module and equals `0.11.1` during the port rather than a
hardcoded PR value.

Add a failing Python generator test that requires deterministic `gen:version`
and non-writing `check:version` behavior sourced from `pyproject.toml`.

- [ ] **Step 2: Write failing Regulus detection tests**

Cover authenticated success, 404 absent, 401/403 hidden, and transport failure.
The tests must import real client helpers and observe their returned state.

- [ ] **Step 3: Write the executable source-parity matrix**

Give every row in `SOURCE-INVENTORY.md` a stable case ID in
`source-parity-cases.ts`. Using Vitest, jsdom, and React Testing Library, cover
every route group and its loading/empty/error state in
`routes-and-states.test.tsx`, every polling and optimistic mutation contract in
`polling-and-mutations.test.tsx`, and Studio drag/edge/publish/clone/deploy/run
eligibility in `studio-behavior.test.tsx`. A completeness assertion fails when
an inventory ID lacks a passing test mapping.

- [ ] **Step 4: Run RED**

Run:

```bash
cd frontend && npm test -- --run
```

Also run `uv run pytest -q tests/scripts/test_frontend_version.py`. Expected:
the new tests fail because the rebuilt navigation, version generator/module,
and Regulus helpers are absent.

- [ ] **Step 5: Commit tests**

Commit the intentionally failing test contract together with no production
files only if repository policy permits red commits; otherwise keep it staged,
record the RED output, and complete Task 3 before the atomic green commit.

### Task 3: Transplant the console and preserve new-core behavior

**Files:**
- Modify/Create: the 57 `frontend/` paths listed by PR #4
- Preserve: `frontend/AGENTS.md`, `frontend/CLAUDE.md`
- Preserve: `frontend/app/webhooks/page.tsx`
- Resolve semantically: `frontend/app/audit/page.tsx`
- Resolve semantically: `frontend/app/components/AppShell.tsx`
- Resolve semantically: `frontend/app/cost/page.tsx`
- Resolve semantically: `frontend/app/lib/api.ts`
- Resolve semantically: `frontend/app/page.tsx`
- Resolve semantically: `frontend/app/retention/page.tsx`
- Resolve semantically: `frontend/app/runs/page.tsx`
- Resolve semantically: `frontend/app/templates/page.tsx`
- Resolve semantically: `frontend/app/lib/regulus.test.ts`

- [ ] **Step 1: Apply the mechanical frontend delta**

Apply only the PR delta from merge base `d574ca8` to source tip `4d39d15`.
Exclude generated API artifacts initially. Bulk application may use Git's
patch machinery; resolve every semantic conflict with `apply_patch`. Preserve
and reconcile the stronger RED `regulus.test.ts` instead of overwriting it with
the source PR's weaker test.

- [ ] **Step 2: Preserve destination-only behavior**

Keep `/webhooks` in navigation and retain refactor-era API additions. Preserve
Studio publish/clone/deploy/run eligibility and the existing React Flow event
handlers while applying the new presentation.

- [ ] **Step 3: Fix dependency and version integration**

Carry required font/package dependencies and lock entries. Implement
`scripts/generate_frontend_version.py`; add `gen:version` and non-writing
`check:version` package commands; generate the frontend version module from
`pyproject.toml`. Do not copy `VERSION = "0.10.6"`.

- [ ] **Step 4: Run GREEN**

Run `uv run pytest -q tests/scripts/test_frontend_version.py`, then
`cd frontend && npm run check:version && npm test -- --run`. Expected: all
generator and parity tests pass.

- [ ] **Step 5: Build and commit**

Run `cd frontend && npm run build`, then commit:

```bash
git commit -m "feat: port rebuilt console to refactored core"
```

### Task 4: Add platform-level Regulus authority with TDD

**Files:**
- Modify: `src/zeroth/governance/identity/models.py`
- Modify: `src/zeroth/service/api/authorization.py`
- Modify: `tests/service/helpers.py`
- Modify: `tests/service/test_authorization.py`
- Modify: `tests/service/test_bearer_auth.py`
- Modify: `tests/contracts/fixtures/backend_surface_canonical.json`
- Modify: `tests/contracts/fixtures/backend_surface_legacy.json`

- [ ] **Step 1: Write failing authorization tests**

Add `ServiceRole.PLATFORM_ADMIN` expectations. Prove ordinary `ADMIN` lacks
`Permission.ECON_ADMIN`, while `PLATFORM_ADMIN` has it and retains normal admin
permissions. Prove trusted static configuration delivers the reserved role. In
`tests/service/test_bearer_auth.py`, prove the role is delivered only through a
cryptographically verified trusted bearer claim and rejected from invalid or
unverified claims.

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest -q tests/service/test_authorization.py tests/service/test_bearer_auth.py
```

Expected: failure because the role and permission do not exist.

- [ ] **Step 3: Implement the minimum authority model**

Add the reserved role and permission. Map `ADMIN` to all permissions except
`ECON_ADMIN`; map `PLATFORM_ADMIN` to the full permission set. Update explicit
test credentials without broadening existing credentials.

- [ ] **Step 4: Update only intentional public-surface fixtures**

Regenerate the evolving canonical fixture if the new public symbol requires it.
Do not change any existing legacy capability signature. Update the immutable
legacy fixture only if a genuinely new protected capability is registered and
only under the documented additive-amendment procedure; enum membership alone
does not authorize a legacy signature edit.

- [ ] **Step 5: Run GREEN and commit**

Run focused authorization, bearer, identity, and architecture surface tests,
then:

```bash
git commit -m "feat: reserve platform authority for Regulus"
```

### Task 5: Build the hardened Regulus proxy with TDD

**Files:**
- Create: `src/zeroth/service/api/regulus_proxy_api.py`
- Modify: `src/zeroth/service/app.py`
- Create: `tests/service/test_regulus_proxy.py`
- Modify: `tests/contracts/fixtures/backend_route_inventory.json`
- Modify: `tests/contracts/fixtures/backend_openapi.json`

- [ ] **Step 1: Write failing authorization and availability tests**

Prove operator and reviewer receive 403. Require same-tenant and foreign-tenant
`ADMIN` static credentials to receive 403 for a representative GET and both
approve/reject POSTs. Prove a trusted statically configured `PLATFORM_ADMIN`
reaches each handler; missing mount or credential provider returns a stable
503.

- [ ] **Step 2: Write failing route-table and canonicalization tests**

Parameterize every allowed GET/POST template. Reject prefix smuggling,
unexpected queries, `auth`, dot segments, `%2e`, `%2f`, `%5c`, repeated
separators, NUL/control characters, unknown methods, and redirects.

- [ ] **Step 3: Write failing body and error tests**

Accept only `{reason: string}` up to 2,000 characters and an 8 KiB raw body.
Reject extra keys and invalid JSON. Assert 502/503 details contain no upstream
exception, URL, token, or response-body text.

- [ ] **Step 4: Run RED**

Run `uv run pytest -q tests/service/test_regulus_proxy.py` and verify the route
module/registration is missing.

- [ ] **Step 5: Implement the exact route table**

Use typed route descriptors, segment-aware matching, raw-path checks,
`follow_redirects=False`, strict Pydantic request validation, bounded reads,
and stable sanitized errors. Register only under `/v1/econ/regulus`; do not add
an unversioned compatibility alias for this global administrative surface.

- [ ] **Step 6: Run GREEN and commit**

Run proxy, app, route-inventory, OpenAPI snapshot, cold-import, and authorization
tests, then:

```bash
git commit -m "feat: expose hardened Regulus console proxy"
```

### Task 6: Rebuild deterministic API generation

**Files:**
- Modify: `scripts/dump_openapi.py`
- Create: `scripts/dump_regulus_openapi.py`
- Create/Modify: `tests/scripts/test_openapi_generation.py`
- Modify: `frontend/package.json`
- Modify: `frontend/openapi.json`
- Modify: `frontend/openapi.regulus.json`
- Modify: `frontend/app/lib/api-types.ts`
- Modify: `frontend/app/lib/api-types.regulus.ts`
- Modify: `.github/workflows/docs.yml`

- [ ] **Step 1: Write failing generator tests**

Assert the main generator imports `zeroth.service.app`, the Regulus generator
uses `zeroth.econ.plane.main`, both outputs are deterministic, and `--check`
detects drift. Add a test proving the parent schema does not pretend to contain
mounted Regulus routes.

- [ ] **Step 2: Run RED**

Run `uv run pytest -q tests/scripts/test_openapi_generation.py`. Expected:
legacy import and missing Regulus generator failures.

- [ ] **Step 3: Implement generators and scripts**

Make `gen:api`, `gen:regulus-api`, and `gen:all-api` deterministic. Add a
`check:api` command that regenerates to temporary files and compares both JSON
and TypeScript outputs without rewriting tracked files.

Retain Task 3's `gen:version` and non-writing `check:version` commands. Update
`.github/workflows/docs.yml` to install the pinned frontend Node dependencies
and run both `npm run check:api` and `npm run check:version`, proving main and
Regulus JSON/TypeScript artifacts plus the version module are clean without
rewriting tracked files.

- [ ] **Step 4: Regenerate and reconcile consumers**

Run `cd frontend && npm run gen:all-api`. Adapt `api.ts`, `regulusApi.ts`, and
pages only where the fresh new-core types differ from the source PR.

- [ ] **Step 5: Verify and commit**

Run generator tests, `npm run check:api`, `npm run check:version`,
`npm test -- --run`, and `npm run build`, then:

```bash
git commit -m "build: generate console contracts from new core"
```

### Task 7: Close semantic parity and integration defects

**Files:**
- Modify: frontend files implicated by failing parity/build tests
- Modify: backend files implicated by proxy/service tests
- Add: focused regression tests adjacent to each defect

- [ ] **Step 1: Compare destination against the source inventory**

Check every row in `SOURCE-INVENTORY.md`; record any missing behavior before
changing code. Run the semantic-port matrix and require its completeness test to
map every inventory row to an executable assertion; a manual checklist alone is
not sufficient.

- [ ] **Step 2: Debug each failure systematically**

For each defect, reproduce it with one focused failing test, identify the
source/new-core contract difference, implement the smallest fix, and rerun the
focused test. Do not bundle unrelated corrections.

- [ ] **Step 3: Commit coherent fixes atomically**

Use one normal commit per defect class, such as:

```bash
git commit -m "fix: preserve console deployment rollback contract"
```

- [ ] **Step 4: Run the integrated frontend/service battery**

Run frontend tests/build plus proxy, app, route inventory, OpenAPI, Studio,
deployment, retention, audit, and authorization suites.

### Task 8: Flip legacy off and prepare v0.12.0 with TDD

**Files:**
- Modify: `src/zeroth/contracts/graph/models.py`
- Create: `src/zeroth/contracts/graph/engine_mode.py`
- Modify: `src/zeroth/contracts/graph/serialization.py`
- Modify: `src/zeroth/contracts/graph/validation/joins.py`
- Modify: `src/zeroth/contracts/graph/validation/token_loops.py`
- Modify: `src/zeroth/runtime/orchestration/driver.py`
- Modify: `src/zeroth/core/orchestrator/runtime.py`
- Modify: `src/zeroth/service/deployments/service.py`
- Modify: `tests/graph/test_models.py`
- Create: `tests/architecture/test_token_engine_mode_access.py`
- Modify: backend surface fixtures
- Modify: token-engine/default-sensitive tests
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/docs/test_release_metadata.py`
- Modify: `CHANGELOG.md`
- Modify: user documentation describing the escape hatch

- [ ] **Step 1: Write the failing default-ON test**

Assert an unauthored `ExecutionSettings()` selects the structured engine through
the shared effective-mode helper, while the pinned public field signature and
attribute default remain `False` for legacy ABI compatibility. Assert the flag
remains absent from serialization when unauthored, explicit `True` remains
token mode, and explicit `False` remains present, warns, and selects legacy.

- [ ] **Step 2: Run RED**

Run the focused graph model and implicit-runtime tests. Expected: the effective
implicit-mode assertion fails because production consumers currently interpret
the raw `False` field instead of authored-field presence.

Add a failing AST architecture test that scans `src/zeroth` and rejects direct
effective-mode reads of `sequential_join_enabled` outside
`contracts/graph/models.py` presence/serialization compatibility code and the
canonical `engine_mode.py` helper.

- [ ] **Step 3: Flip the default and reconcile fixtures**

Do not change the pinned `ExecutionSettings` parameter type or default. Add one
canonical helper whose effective semantics are: absent flag means token mode,
explicit `True` means token mode, explicit `False` means legacy. Route every
runtime, validator, deployment, serialization, and hydration consumer through
that helper so no production decision reads the raw field directly. Preserve
absent/true/false serialization, deployment pinning, v1 attestation
compatibility, migration backfill, and pin-aware hydration. The immutable
legacy signatures remain byte-identical; any unavoidable surface-policy change
requires a separately approved policy amendment before code changes.

The migration must explicitly replace raw decisions in
`service/deployments/service.py`, `contracts/graph/serialization.py`,
`contracts/graph/validation/joins.py`,
`contracts/graph/validation/token_loops.py`,
`runtime/orchestration/driver.py`, and `core/orchestrator/runtime.py`. The AST
guard prevents new bypasses after this enumerated migration.

- [ ] **Step 4: Update release metadata**

Set project, lockfile, release test, frontend generated version, and changelog
to `0.12.0`. Document explicit `sequential_join_enabled=False` as the temporary
legacy escape hatch.

- [ ] **Step 5: Run GREEN and commit**

Run default-sensitive, deployment, attestation, serialization, legacy marker,
architecture, and release metadata tests, then:

```bash
git commit -m "release: enable structured token engine by default"
```

### Task 9: Execute all release gates and independent review

**Files:**
- Create only if required: deterministic checker reports outside tracked tree
- Modify only through new TDD cycles if a gate exposes a defect

- [ ] **Step 1: Run frontend gates**

Run:

```bash
cd frontend && npm ci
cd frontend && npm test -- --run
cd frontend && npm run check:api
cd frontend && npm run check:version
cd frontend && npm run build
```

- [ ] **Step 2: Run backend and compatibility gates**

Run:

```bash
uv run pytest -q
uv run pytest -q tests/orchestrator -m legacy_engine
uv run ruff check src/ tests/
```

- [ ] **Step 3: Run checker gates**

Run the exact N=4 exhaustive and N=5/N=6 10,000-case commands from the release
spec, preserving counts and runtimes.

- [ ] **Step 4: Run architecture/cold-import gates**

Run the overlay's canonical/legacy surface, route inventory, repository edge,
lazy attribute, and cold-import batteries in fresh processes.

- [ ] **Step 5: Freeze a clean candidate SHA**

Require a clean worktree after all executable gates and record
`<candidate-release-sha>`. No review may refer to a branch name or mutable HEAD.

- [ ] **Step 6: Obtain independent adversarial review**

Give a reviewer who implemented none of the changes only the candidate SHA, design,
plan, source tip, and gate outputs. Resolve every HIGH finding through TDD and
repeat all affected gates plus a fresh independent review of the new SHA. Every
commit invalidates the prior review. Do not self-certify.

- [ ] **Step 7: Promote the approved immutable identity**

After review reports zero unresolved HIGH findings for the exact clean
`<candidate-release-sha>`, set `<tested-release-sha>` to that same object ID.
Rerun a short SHA-sensitive smoke check; no commits may occur afterward without
invalidating the gates, review, and release identity.

### Task 10: Preserve main remotely and cut over

**Files:** None

- [ ] **Step 1: Re-fetch and validate old main**

Fetch `origin/main`. If it differs from the reviewed expected old-main SHA,
stop and review the new commits before any push.

- [ ] **Step 2: Verify/create the remote backup**

Check `refs/heads/codex/pre-refactor-main-20260721`. Abort on a mismatched
existing ref. Create it from `<old-main-sha>` only with an absence lease, or
reuse it if equal. Fetch and verify both local and remote backup refs.

- [ ] **Step 3: Force-with-lease the immutable tested SHA**

Run the exact spec command using `<old-main-sha>` and
`<tested-release-sha>`. Never use raw `--force` or mutable `HEAD`.

- [ ] **Step 4: Verify remote main and CI**

Fetch main; require its OID and tree to equal `<tested-release-sha>`. Monitor
all main-triggered required checks and release smoke checks for that exact SHA.
Stop cleanup if any fail.

### Task 11: Close superseded PR #4 safely

**Files:** None

- [ ] **Step 1: Re-fetch and validate PR #4 head**

Require both GitHub and the fetched remote branch to equal
`4d39d15abab3608322ca20ee55a99f95c46607c5`. If either reveals a newer tip,
fetch the exact GitHub PR head ref/OID, verify the object exists locally, then
create `codex/preserve-console-rebuild-<short-sha>` at that object. Abort if the
preservation name already exists at a different OID. Verify the new local ref,
then abort cleanup for review. Do not close or delete the PR branch.

- [ ] **Step 2: Close PR #4 with replacement evidence**

Comment with `<tested-release-sha>`, summarized verification counts, and the
fact that the semantic behavior was ported. Close PR #4.

- [ ] **Step 3: Delete only the verified source branch**

Delete `feat/console-rebuild` with a lease tied to the freshly verified PR head.
Verify the remote ref is absent and `codex/preserve-console-rebuild` still
contains the source tip.

- [ ] **Step 4: Preserve unrelated collaboration state**

Confirm PR #5 remains open and merged PR #3 remains recorded. Report backup,
replacement, PR, CI, test/checker counts, runtimes, and rollback SHAs.

- [ ] **Step 5: Keep the exact rollback procedure ready**

If a post-cutover release blocker requires rollback, run:

```bash
git push --force-with-lease=refs/heads/main:<tested-release-sha> \
  origin <old-main-sha>:refs/heads/main
git fetch origin main
git rev-parse origin/main
```

Require the fetched OID to equal `<old-main-sha>` and report the rollback.

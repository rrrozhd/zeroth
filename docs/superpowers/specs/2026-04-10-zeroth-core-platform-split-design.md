# Zeroth Core/Platform Split — Design

**Date:** 2026-04-10
**Status:** Draft, awaiting approval
**Author:** Claude + dondoe

## Motivation

Zeroth's core engine (graph, orchestrator, contracts, runs, etc.) is currently tangled with platform code (FastAPI service, Studio, storage drivers, migrations) in a single `src/zeroth/` tree. The user now wants to build a separate application *on top of* the core and serve it as its own API service. That is blocked today because:

1. `zeroth/__init__.py` re-exports from `zeroth.core.storage`, so any `import zeroth.*` transitively loads psycopg, SQLAlchemy, Redis, Alembic — even when the consumer only wants `Graph`.
2. `GraphRepository`, `RunRepository`, `AuditRepository` require a live `AsyncDatabase`. There is no in-memory fallback, so `RuntimeOrchestrator` cannot run without standing up storage.

Good news from the coupling audit: no core module imports FastAPI or a DB driver directly, and no core module reverse-imports from `service`/`studio`/`admin`. `RuntimeOrchestrator` is a plain dataclass with all dependencies injected. The surgery is mostly mechanical.

## Goals

- Ship `zeroth-core` as an independent, pip-installable Python SDK.
- `import zeroth.core.*` must not transitively load FastAPI, SQLAlchemy, psycopg, Alembic, Redis, Arq, Chroma, Elasticsearch, or pgvector.
- A user can `pip install zeroth-core`, build a `Graph` programmatically, and execute a run in-process with no DB.
- `zeroth-platform` continues to exist as its own repo, depending on `zeroth-core`. Not shippable yet — CI tests/lints only.
- Hard enforcement of the boundary via `importlinter` in CI.
- Preserve git history per-file via `git filter-repo`.

## Non-goals

- Publishing `zeroth-platform` to PyPI.
- Decoupling `zeroth-core` from `governai` (still a core dependency).
- Rewriting any business logic. This is a move + seal, not a rewrite.

## PyPI name availability (verified 2026-04-10)

| Name | Status | Decision |
|---|---|---|
| `zeroth-core` | available | ✅ reserved for SDK |
| `zeroth-platform` | available | ✅ reserved for platform (future publish) |
| `zeroth` | taken (not by us) | not used |
| `econ-instrumentation-sdk` | available | ✅ keep current name; publish under this |
| `regulus` | taken (not by us) | not used |
| `regulus-sdk` | available | fallback if rename desired later |

Regulus SDK keeps its current package name `econ-instrumentation-sdk` — unchanged from the local `pyproject.toml`. Only the distribution changes (git+path → PyPI).

## Final layout

Parent directory `/Users/dondoe/coding/zeroth/` contains two independent git repos as subdirs. The current repo is moved to `/Users/dondoe/coding/zeroth-archive/` and archived on GitHub (read-only).

```
/Users/dondoe/coding/zeroth/           # parent dir (not a repo)
├── zeroth-core/                        # independent git repo (public)
│   ├── pyproject.toml                  # name = "zeroth-core"
│   ├── src/zeroth/core/                # PEP 420 namespace subpackage
│   │   ├── graph/
│   │   ├── orchestrator/
│   │   ├── contracts/
│   │   ├── runs/                       # models, protocols, in-memory repo
│   │   ├── execution_units/
│   │   ├── agent_runtime/              # runner, thread resolver protocol
│   │   ├── mappings/
│   │   ├── conditions/
│   │   ├── approvals/                  # models, service, protocol
│   │   ├── audit/                      # models, protocol
│   │   ├── policy/
│   │   ├── secrets/                    # protocols, env/local resolver
│   │   ├── memory/                     # protocols, in-memory
│   │   └── guardrails/                 # pure rule types
│   ├── tests/                          # no DB required
│   ├── docs/                           # SDK quickstart, API reference
│   ├── phases/                         # core-scoped phase docs only
│   ├── PROGRESS.md                     # core-scoped roadmap
│   ├── CLAUDE.md                       # core-scoped agent guidelines
│   ├── README.md
│   └── .github/workflows/              # ci.yml, publish.yml
│
└── zeroth-platform/                    # independent git repo (public)
    ├── pyproject.toml                  # name = "zeroth-platform"
    ├── src/zeroth/platform/
    │   ├── service/                    # FastAPI app, routers, auth
    │   ├── studio_api/                 # studio backend endpoints
    │   ├── admin_api/
    │   ├── storage/                    # AsyncDatabase, Postgres/SQLite, Redis
    │   ├── migrations/                 # alembic
    │   ├── repositories/               # SQL-backed impls of core protocols
    │   ├── dispatch/                   # arq worker
    │   ├── webhooks/
    │   ├── observability/
    │   ├── deployments/
    │   ├── identity/
    │   ├── econ/
    │   ├── sandbox_sidecar/
    │   ├── memory/                     # pgvector, chroma, ES backends
    │   ├── guardrails/                 # rate limiter (Redis)
    │   └── config/
    ├── apps/studio/                    # React frontend
    ├── tests/                          # full-stack
    ├── docs/
    ├── phases/                         # platform-scoped phase docs
    ├── .planning/                      # gsd:* state (moved from current repo)
    ├── PROGRESS.md
    ├── CLAUDE.md
    ├── README.md
    └── .github/workflows/              # ci.yml only — no publish
```

### Namespace mechanics

- Both repos ship into a **PEP 420 implicit namespace package** named `zeroth`. Neither repo contains a top-level `src/zeroth/__init__.py`.
- `zeroth-core` owns `zeroth.core.*`. `zeroth-platform` owns `zeroth.platform.*`.
- When both are installed in the same environment, Python resolves `zeroth.core` and `zeroth.platform` from separate wheels without collision.
- The current re-exporting `src/zeroth/__init__.py` is **deleted** — it's incompatible with namespace packages.

## Module-by-module split

The following table maps every module in the current `src/zeroth/` to its destination repo. Most modules have a pure `models.py` / `service.py` that lands in core and a specific `repository.py` / `store.py` / `connector.py` that lands in platform.

| Current module | → `zeroth-core/src/zeroth/core/` | → `zeroth-platform/src/zeroth/platform/` |
|---|---|---|
| `graph/` | models, validation, diff, serialization, versioning, errors, storage constants, protocol | `repository.py` (DB-backed) |
| `orchestrator/` | all (runtime.py is pure) | — |
| `contracts/` | all | — |
| `runs/` | `models.py`, protocol, in-memory repo | `repository.py` |
| `execution_units/` | all | — |
| `agent_runtime/` | runner, resolver protocol, in-memory thread resolver | `thread_store.py` |
| `mappings/` | all | — |
| `conditions/` | all | — |
| `approvals/` | models, service, protocol | `repository.py` |
| `audit/` | models, protocol | `repository.py`, `verifier.py` |
| `policy/` | all | — |
| `secrets/` | protocols, env/local resolver | cloud KMS backends if any |
| `memory/` | protocols, in-memory | `pgvector_connector.py`, chroma, ES |
| `guardrails/` | rule types, pure validators | `rate_limit.py` |
| `storage/` | — | all (DB drivers, Redis, pooling) |
| `migrations/` | — | all (alembic) |
| `dispatch/` | — | all (arq worker) |
| `webhooks/` | — | all |
| `observability/` | — | all |
| `deployments/` | — | all |
| `identity/` | — | all (JWT/auth) |
| `econ/` | — | all (Regulus integration) |
| `sandbox_sidecar/` | — | all (FastAPI sub-app) |
| `config/` | minimal core settings if any | platform config |
| `service/` | — | all (FastAPI app) |
| `studio/` | — | all (Studio backend helpers) |
| `demos/` | — | all |
| `apps/studio/` (React) | — | all |

### Import rules (enforced)

`zeroth.core.*` may **not** import:
- `zeroth.platform.*`
- `fastapi`, `sqlalchemy`, `psycopg`, `alembic`
- `redis`, `arq`, `chromadb`, `elasticsearch`, `pgvector`

`zeroth.core.*` **may** import:
- `governai`, `pydantic`, `pydantic-settings`, `httpx`, stdlib
- `langchain-litellm` / `litellm` (LLM abstraction, pure Python)

`zeroth.platform.*` may freely import `zeroth.core.*`.

Enforced via `importlinter` in both repos' CI:

```toml
[tool.importlinter]
root_packages = ["zeroth"]

[[tool.importlinter.contracts]]
name = "core does not depend on platform or IO stacks"
type = "forbidden"
source_modules = ["zeroth.core"]
forbidden_modules = [
  "zeroth.platform",
  "fastapi", "sqlalchemy", "psycopg", "alembic",
  "redis", "arq", "chromadb", "elasticsearch", "pgvector",
]
```

## Persistence & repository protocols

Core defines `Protocol` types for every repository and ships trivial in-memory implementations. Platform ships SQL/Redis-backed implementations that satisfy the same protocols.

**Example — graph repository:**

```python
# zeroth/core/graph/protocols.py
from typing import Protocol, Sequence
from zeroth.core.graph.models import Graph

class GraphRepository(Protocol):
    async def save(self, graph: Graph) -> Graph: ...
    async def get(self, graph_id: str, version: int | None = None) -> Graph | None: ...
    async def list(self) -> Sequence[Graph]: ...
    async def publish(self, graph_id: str, version: int) -> Graph: ...
    async def archive(self, graph_id: str) -> None: ...

# zeroth/core/graph/memory_repository.py
class InMemoryGraphRepository:
    """Dict-backed graph repository. For tests, notebooks, embedded use."""
    def __init__(self) -> None:
        self._store: dict[tuple[str, int], Graph] = {}
    # ... implements the Protocol
```

```python
# zeroth/platform/repositories/graph_repository.py
class SqlGraphRepository:
    """Moved from the current zeroth/graph/repository.py, unchanged logic."""
    def __init__(self, database: AsyncDatabase) -> None: ...
```

Same pattern applies to:
- `RunRepository`
- `AuditRepository`
- `ApprovalRepository`
- `ThreadResolver`
- `MemoryStore`
- `DeploymentRepository` (platform-only — no in-memory version needed)

### Embedded use pattern (the user's target workflow)

```python
from zeroth.core.graph import Graph, AgentNode, Edge
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import InMemoryRunRepository
from zeroth.core.agent_runtime import AgentRunner

class MyAgent(AgentRunner):
    async def run(self, ...): ...

graph = Graph(
    graph_id="my-app",
    nodes=[AgentNode(...)],
    edges=[Edge(...)],
    ...
)

orchestrator = RuntimeOrchestrator(
    run_repository=InMemoryRunRepository(),
    agent_runners={"my_agent": MyAgent()},
    executable_unit_runner=...,
)

result = await orchestrator.execute(graph, input={...})
```

No FastAPI, no database, no Alembic bootstrap, no config files.

## Migration strategy

### Step 0 — Preserve ALL work (prerequisite, highest priority)

The current repo has **36 worktrees** under `.claude/worktrees/agent-*` plus the main working tree. User directive: "definitely save all the work we've done on zeroth so far in an archive" — **nothing is discarded, nothing is irreversibly touched without a safety net**.

**Four-layer preservation strategy.** Each layer is an independent snapshot; if any later step goes wrong, any of the prior layers is a complete recovery source.

#### Layer 1 — Filesystem tarball (zero-interpretation snapshot)

Before any git operation touches the repo, take a bit-for-bit tarball of the entire directory:

```bash
cd /Users/dondoe/coding
tar --exclude='zeroth/node_modules' \
    --exclude='zeroth/apps/studio/node_modules' \
    --exclude='zeroth/.venv' \
    --exclude='zeroth/**/__pycache__' \
    -czf zeroth-snapshot-$(date +%Y%m%d-%H%M%S).tar.gz zeroth/
ls -lh zeroth-snapshot-*.tar.gz
sha256sum zeroth-snapshot-*.tar.gz > zeroth-snapshot-$(date +%Y%m%d-%H%M%S).sha256
```

This captures **everything**: git objects, all 36 worktree working trees (committed + uncommitted + untracked), `.planning/`, `phases/`, `PROGRESS.md`, every in-flight edit, `.claude/` session state — all of it, frozen at this exact moment. `node_modules` and `__pycache__` are excluded because they're rebuildable noise; the `.venv` exclusion is optional (uv lockfiles are committed).

**Verification:** tarball size sanity check (expect several hundred MB including git history), sha256 recorded. The tarball is stored at `/Users/dondoe/coding/zeroth-snapshot-YYYYMMDD-HHMMSS.tar.gz` — outside the repo, will not be touched by any subsequent step. **User visually confirms this file exists and is non-empty before we proceed to Layer 2.** (Gate.)

#### Layer 2 — Bare mirror clone (git-native snapshot)

```bash
cd /Users/dondoe/coding
git clone --mirror zeroth zeroth-mirror.git
```

`--mirror` captures every ref: all branches (including all 36 worktree branches), all tags, the reflog, HEAD. No working tree, pure object database. This is the canonical git-native backup. If the main repo's history gets corrupted or the wrong thing gets force-pushed, `zeroth-mirror.git` can be cloned back into a fresh working copy with `git clone zeroth-mirror.git zeroth-restored`.

**Verification:** `cd zeroth-mirror.git && git branch -a | wc -l` ≥ current branch count, `git log --all --oneline | wc -l` matches the source repo's total commit count. (Automated check.)

#### Layer 3 — Commit all in-flight state & push to current GitHub remote

Only after Layers 1 and 2 exist do we touch the live repo.

1. **Worktree sweep.** For each worktree returned by `git worktree list` (excluding the main `/Users/dondoe/coding/zeroth` entry):
   ```bash
   for wt in $(git worktree list --porcelain | awk '/^worktree /{print $2}' | grep worktrees/agent-); do
     (
       cd "$wt"
       if [ -n "$(git status --porcelain)" ]; then
         git add -A
         git commit -m "wip: archive snapshot before core/platform split" || true
       fi
     )
   done
   ```
   Every worktree's uncommitted changes and untracked files become a commit on whatever branch it has checked out. Scripted, idempotent, nothing discarded.

2. **Main working tree sweep.** Same treatment for `/Users/dondoe/coding/zeroth`: commit `.planning/STATE.md`, `.planning/config.json`, `dev_server.py`, `live_test.py`, `src/zeroth/demos/`, and everything else currently modified or untracked — **except** `zeroth.db`, `zeroth_dev.db`, `zeroth_live.db`, which are added to `.gitignore` (if not already) and **not** committed. Local database files are ephemeral runtime state, not work product.

3. **Push everything to the current `origin`.** Before any rename:
   ```bash
   git push origin --all
   git push origin --tags
   git push origin --mirror    # belt-and-suspenders: also pushes deleted refs sync, but see note
   ```
   Note: `--mirror` will also delete remote refs that don't exist locally, so only run the first two lines unless explicitly desired. This ensures the remote is a complete snapshot including every worktree branch.

4. **Do NOT remove worktrees.** Their working-tree state stays on disk; their branches stay in the refs. The archive directory keeps both reachable via `git worktree list`.

5. **Verify.**
   - `git status` in main tree shows only the three `.db` files as ignored.
   - `git worktree list` still shows all 36.
   - `git branch -a | wc -l` — record count.
   - `git log --oneline origin/main..HEAD` — should be empty (main is pushed).
   - For each worktree branch `B`: `git log --oneline origin/$B..$B` should be empty.

#### Layer 4 — GitHub archive (remote, read-only)

After Layer 3's sweep + push, the current `rrrozhd/zeroth` GitHub repo contains everything. Then and only then:

1. Rename `rrrozhd/zeroth` → `rrrozhd/zeroth-archive` via `gh api -X PATCH repos/rrrozhd/zeroth -f name=zeroth-archive`.
2. Mark it archived: `gh api -X PATCH repos/rrrozhd/zeroth-archive -F archived=true`. This makes the entire repo read-only on GitHub — no pushes, no issues, no PRs, no accidents.
3. Verify: `gh repo view rrrozhd/zeroth-archive --json isArchived`.

If `rrrozhd/zeroth` does not exist on GitHub currently, Layer 4 is skipped — Layers 1 and 2 (local tarball + local bare mirror) are sufficient local preservation. (User confirms whether remote exists.)

#### Preservation gate

Before the local path rename in Step 1 happens, **all four layers must be verified**:

| Layer | Artifact | Verification |
|---|---|---|
| 1 | `/Users/dondoe/coding/zeroth-snapshot-*.tar.gz` | file exists, size > 100 MB, sha256 recorded, user has visually confirmed |
| 2 | `/Users/dondoe/coding/zeroth-mirror.git/` | `git log --all --oneline \| wc -l` matches source |
| 3 | origin remote on GitHub (if exists) | `git push` exit code 0 on all branches |
| 4 | `rrrozhd/zeroth-archive` archived on GitHub (if applies) | `isArchived == true` |

I will explicitly print these verifications and wait for user "proceed" before Step 1. No exceptions.

### Step 1 — Archive the old repo

**Local:** move `/Users/dondoe/coding/zeroth/` → `/Users/dondoe/coding/zeroth-archive/`. The directory and full git history are preserved intact as a reference. All 36 worktrees remain attached (their paths inside `.claude/worktrees/agent-*` move with the parent directory). `git worktree list` in the archive will still resolve them correctly because worktree pointers are relative to `.git/worktrees/*/gitdir`, which is inside the moved tree.

**GitHub:** rename the current `rrrozhd/zeroth` repo to `rrrozhd/zeroth-archive`, then toggle the "Archive this repository" setting (makes it read-only). If there is no current `rrrozhd/zeroth` on GitHub, this step is skipped. The rename preserves all branches, PRs, issues, and the full commit graph.

After this step, `/Users/dondoe/coding/zeroth/` no longer exists. The new parent directory is created empty at the same path, ready for the two subrepos to be cloned into it.

### Step 2 — Create empty GitHub repos

```bash
gh repo create rrrozhd/zeroth-core --public \
  --description "Zeroth SDK: governed multi-agent graphs as a Python library"
gh repo create rrrozhd/zeroth-platform --public \
  --description "Zeroth platform: service, storage, studio built on zeroth-core"
```

Both repos start empty (no auto-init — we'll push from filter-repo'd clones).

### Step 3 — History split with `git filter-repo`

Two independent passes against clones of the archive.

**Pass A — zeroth-core** (keep paths, rename into `zeroth/core/`):

```bash
cd /tmp
git clone /Users/dondoe/coding/zeroth-archive zeroth-core-build
cd zeroth-core-build
git filter-repo \
  --path src/zeroth/graph/ \
  --path src/zeroth/orchestrator/ \
  --path src/zeroth/contracts/ \
  --path src/zeroth/runs/models.py \
  --path src/zeroth/runs/__init__.py \
  --path src/zeroth/execution_units/ \
  --path src/zeroth/agent_runtime/ \
  --path src/zeroth/mappings/ \
  --path src/zeroth/conditions/ \
  --path src/zeroth/approvals/models.py \
  --path src/zeroth/approvals/service.py \
  --path src/zeroth/audit/models.py \
  --path src/zeroth/policy/ \
  --path src/zeroth/secrets/ \
  --path src/zeroth/memory/models.py \
  --path src/zeroth/guardrails/models.py \
  --path tests/ \
  --path docs/ \
  --path pyproject.toml \
  --path README.md \
  --path CLAUDE.md \
  --path-rename src/zeroth/:src/zeroth/core/
```

Then manually scrub and rewrite:
- `pyproject.toml` → new minimal core deps
- `CLAUDE.md` → core-scoped
- `README.md` → SDK quickstart
- `tests/` → drop any test file that touches DB repositories (keeps only pure tests)
- Add protocols, in-memory repos, `importlinter` config
- Drop `runs/repository.py`, `audit/repository.py`, `approvals/repository.py` after the rename (they slipped in with the parent-directory path matches; `filter-repo` is conservative, so use `--invert-paths` in a second pass if needed, or remove with a regular commit).

**Pass B — zeroth-platform** (drop pure core files, rewrite into `zeroth/platform/`):

```bash
cd /tmp
git clone /Users/dondoe/coding/zeroth-archive zeroth-platform-build
cd zeroth-platform-build
git filter-repo \
  --invert-paths \
  --path src/zeroth/graph/models.py \
  --path src/zeroth/graph/validation.py \
  --path src/zeroth/graph/diff.py \
  --path src/zeroth/graph/serialization.py \
  --path src/zeroth/graph/versioning.py \
  --path src/zeroth/graph/errors.py \
  --path src/zeroth/orchestrator/ \
  --path src/zeroth/contracts/ \
  # ... all pure core paths inverted out
```

Then rename remaining `src/zeroth/` to `src/zeroth/platform/`:

```bash
git filter-repo --path-rename src/zeroth/:src/zeroth/platform/ --force
```

Manually scrub and rewrite:
- `pyproject.toml` → platform deps + `zeroth-core @ git+https://github.com/rrrozhd/zeroth-core@main`
- Update every internal import from `zeroth.core.graph` → `zeroth.core.graph`, `zeroth.core.orchestrator` → `zeroth.core.orchestrator`, etc. (scriptable: ruff's `--fix` with import rewrite, or `sed` pass, or a small codemod script).
- `apps/studio/` stays in place, no path rewrite needed.

**Expected complication:** a handful of files will need one-off human decisions (e.g., `runs/__init__.py` re-exports from both `models` and `repository`). Budget 1–2 iterations of the filter-repo passes to get these clean.

### Step 4 — Push to GitHub

```bash
cd /tmp/zeroth-core-build
git remote add origin https://github.com/rrrozhd/zeroth-core.git
git push -u origin main

cd /tmp/zeroth-platform-build
git remote add origin https://github.com/rrrozhd/zeroth-platform.git
git push -u origin main
```

### Step 5 — Clone into the new parent directory

```bash
mkdir -p /Users/dondoe/coding/zeroth
cd /Users/dondoe/coding/zeroth
gh repo clone rrrozhd/zeroth-core
gh repo clone rrrozhd/zeroth-platform
```

### Step 6 — Verify both repos build & test

In `zeroth-core/`:
```bash
uv sync
uv run pytest -v     # must pass with no DB
uv run ruff check src/
uv run importlinter
uv build             # produces wheel
```

In `zeroth-platform/`:
```bash
uv sync              # pulls zeroth-core from git
uv run pytest -v     # full stack, needs postgres+redis
uv run ruff check src/
uv run importlinter
```

### Step 6.5 — Publish regulus SDK to PyPI

The Regulus econ-instrumentation SDK at `/Users/dondoe/coding/regulus/sdk/python/` gets published so that `zeroth-platform` no longer depends on a local `file:///` path. Confirmed: regulus is **local only, no GitHub remote yet**.

Current state:
- Package name: `econ-instrumentation-sdk` (confirmed available on PyPI).
- Version: `0.1.1` in local `pyproject.toml`.
- Build backend: setuptools (pure Python, no extensions).
- The regulus monorepo (`/Users/dondoe/coding/regulus/`) contains `backend/`, `dashboard/`, `demo/`, `docs/`, `infra/`, `sdk/` — but we only publish from `sdk/python/`.
- Git state: unknown whether the regulus directory is even a git repo yet. Must be checked before any archive or publish.

Procedure:

1. **Preserve regulus first.** Apply the same Layer 1 tarball snapshot to regulus:
   ```bash
   cd /Users/dondoe/coding
   tar --exclude='regulus/**/node_modules' \
       --exclude='regulus/**/__pycache__' \
       --exclude='regulus/**/.venv' \
       --exclude='regulus/sdk/python/build' \
       --exclude='regulus/sdk/python/dist' \
       -czf regulus-snapshot-$(date +%Y%m%d-%H%M%S).tar.gz regulus/
   ```
   Gate: user confirms tarball exists.

2. **Initialize or verify the regulus git repo.** Inside `/Users/dondoe/coding/regulus/`:
   ```bash
   if [ ! -d .git ]; then
     git init
     git add -A
     git commit -m "initial import"
   else
     # commit any outstanding work
     git add -A && git commit -m "wip: snapshot before PyPI publish" || true
   fi
   ```
   (Gate: user confirms commit landed, reviews what was committed.)

3. **Create public GitHub repo.**
   ```bash
   gh repo create rrrozhd/regulus --public \
     --description "Regulus: economic instrumentation SDK and stack" \
     --source /Users/dondoe/coding/regulus \
     --push
   ```
   (Gate: user confirms the repo is visible on GitHub and looks right — no secrets, no local DBs leaked.)

4. **Publish workflow.** Add `.github/workflows/publish-sdk.yml` inside `regulus/` that triggers on tags matching `sdk-v*.*.*`, builds from `sdk/python/`, and publishes via PyPI trusted publishing (OIDC). Commit and push.

5. **User action on pypi.org:** configure the trusted publisher for `econ-instrumentation-sdk` — owner `rrrozhd`, repo `regulus`, workflow `publish-sdk.yml`, environment `pypi`. (This is a manual step I document; I cannot execute it.)

6. **Publish flow:**
   - First: point workflow at TestPyPI. Tag `sdk-v0.1.1-test1`. Verify `pip install --index-url https://test.pypi.org/simple/ econ-instrumentation-sdk` installs and imports in a clean venv.
   - Then: flip workflow to prod PyPI. Tag `sdk-v0.1.1`. Verify `pip install econ-instrumentation-sdk` works.

7. **Update `zeroth-platform/pyproject.toml`** to depend on `econ-instrumentation-sdk >= 0.1.1` instead of the `file:///` path.

This step has several manual gates — I will not create the regulus GitHub repo or push anything without explicit user confirmation at each gate. It runs in parallel with zeroth-core publish prep; zeroth-platform CI cannot go green until Step 6.5 completes and the dep is switched.

### Step 7 — Fix up planning docs

Both repos get their planning artifacts rewritten to reference the new structure.

**`zeroth-core/PROGRESS.md`** — new, minimal:
- Section "SDK release v0.1.0"
  - [ ] Clean pyproject.toml, minimal deps
  - [ ] Add protocols + in-memory repos for Graph/Run/Audit/Approval/Thread/Memory
  - [ ] Quickstart doc with embedded example
  - [ ] API reference (auto-gen or hand-written for top-level)
  - [ ] CI green (tests + lint + importlinter)
  - [ ] `uv build` produces wheel
  - [ ] TestPyPI publish + verify `pip install` works
  - [ ] PyPI trusted publisher configured on pypi.org
  - [ ] Tag `v0.1.0`, publish via OIDC

**`zeroth-core/phases/`** — trimmed to phases relevant to the SDK only (graph, orchestrator, contracts, mappings, conditions, execution_units, agent_runtime). Phase docs keep their historical content but path references (`src/zeroth/graph/...`) are rewritten to `src/zeroth/core/graph/...`.

**`zeroth-platform/PROGRESS.md`** — keeps most of the current content, with:
- Every `src/zeroth/X/` reference rewritten to `src/zeroth/platform/X/` for platform modules or `src/zeroth/core/X/` (consumed from `zeroth-core`) for core modules.
- A new top section: "Depends on zeroth-core — pinned to git main, switches to PyPI after core v0.1.0 release."

**`zeroth-platform/phases/`** — keeps every current phase doc, path references rewritten, phases that were split across core+platform get a note "core portion completed in zeroth-core; see that repo's phases/".

**`zeroth-platform/.planning/`** — the current `gsd:*` workflow state moves here wholesale. STATE.md paths rewritten. Active milestone continues.

**CLAUDE.md** — split:
- `zeroth-core/CLAUDE.md`: short, SDK-focused. "This is a pure Python SDK. No FastAPI. No DB. Protocols + pure logic. Tests must pass with zero infra."
- `zeroth-platform/CLAUDE.md`: current one adapted, with path updates and a note about the core dep.

The "fix up plans and roadmaps" step produces one commit per repo: `docs: restructure planning for core/platform split`.

## CI & publish

### zeroth-core `.github/workflows/ci.yml`

```yaml
name: ci
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --dev
      - run: uv run ruff check src/ tests/
      - run: uv run ruff format --check src/ tests/
      - run: uv run importlinter
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --dev
      - run: uv run pytest -v
  build:
    runs-on: ubuntu-latest
    needs: [lint, test]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv build
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
```

### zeroth-core `.github/workflows/publish.yml`

```yaml
name: publish
on:
  push:
    tags: ["v*.*.*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write  # required for OIDC trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

**Pre-requisite** (user action on pypi.org):
1. Log into pypi.org.
2. Account settings → Publishing → Add trusted publisher.
3. PyPI project name: `zeroth-core`.
4. Owner: `rrrozhd`. Repository: `zeroth-core`. Workflow: `publish.yml`. Environment: `pypi`.
5. First release to TestPyPI first — repeat the above on test.pypi.org and point the workflow at TestPyPI until verified, then switch.

### zeroth-platform `.github/workflows/ci.yml`

```yaml
name: ci
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --dev
      - run: uv run ruff check src/ tests/
      - run: uv run importlinter
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready"
          --health-interval 10s
      redis:
        image: redis:7
        ports: ["6379:6379"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --dev
      - run: uv run pytest -v
  studio-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: cd apps/studio && npm ci && npm run build
```

No publish workflow — platform is not shippable.

## Testing

### zeroth-core test suite

- Every existing test under `tests/` that touches pure-core modules is moved (via `filter-repo`) to `zeroth-core/tests/`.
- Any test that uses `AsyncDatabase`, `GraphRepository`, `RunRepository` as concrete DB types is **rewritten** to use the new in-memory implementations.
- Tests that inherently require DB (integration tests for `SqlGraphRepository`) move to platform.
- Target: `uv run pytest -v` in `zeroth-core/` passes with zero infrastructure, no env vars, no containers.

### zeroth-platform test suite

- Everything else. Full existing stack: postgres, redis, alembic migrations, fixtures.
- Platform tests may import both `zeroth.platform.*` and `zeroth.core.*`.
- `tests/conftest.py` pulls in the DB fixtures as today.

## Dependency stacks

### zeroth-core `pyproject.toml` (abbreviated)

```toml
[project]
name = "zeroth-core"
version = "0.1.0"
description = "Zeroth SDK: governed multi-agent graphs as a Python library"
requires-python = ">=3.12"
dependencies = [
  "governai @ git+https://github.com/rrrozhd/governai.git@7452de4db144e8c92b7b5ae0a646c40314a6aa99",
  "pydantic>=2.10",
  "pydantic-settings>=2.13",
  "httpx>=0.27",
  "langchain-litellm>=0.3.4",
  "litellm>=1.83,<2.0",
  "tenacity>=8.2",
  "cachetools>=5.5",
]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.24",
  "ruff>=0.7",
  "importlinter>=2",
  "mypy>=1.13",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/zeroth"]
```

Gone from core: `fastapi`, `psycopg`, `sqlalchemy`, `alembic`, `aiosqlite`, `redis`, `arq`, `chromadb-client`, `elasticsearch`, `pgvector`, `uvicorn`, `PyJWT`, `PyYAML`, `mcp`, `econ-instrumentation-sdk`.

### zeroth-platform `pyproject.toml` (abbreviated)

```toml
[project]
name = "zeroth-platform"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "zeroth-core @ git+https://github.com/rrrozhd/zeroth-core.git@main",
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "psycopg[binary]>=3.3",
  "psycopg-pool>=3.2",
  "aiosqlite>=0.22",
  "alembic>=1.18",
  "sqlalchemy>=2.0",
  "redis>=5.0",
  "arq>=0.27",
  "pgvector>=0.4.2",
  "chromadb-client>=1.5.6",
  "elasticsearch[async]>=8.0,<9",
  "PyJWT[crypto]>=2.10",
  "PyYAML>=6.0",
  "python-dotenv>=1.0",
  "mcp>=1.7,<2.0",
  "econ-instrumentation-sdk >= 0.1.1",
]
```

After first `zeroth-core` PyPI release, the git URL flips to `zeroth-core >= 0.1.0`. The `econ-instrumentation-sdk` dep is already PyPI-based after Step 6.5.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `filter-repo` misses files or keeps junk | Run dry passes on `/tmp` clones first; diff the resulting trees against the expected module map before pushing anywhere. Two iterations budgeted. |
| Circular internal imports surface when modules move | Ran import coupling audit already — no reverse imports found. Any that surface get fixed during the rename pass as regular import edits. |
| `zeroth.core.*` tests import something that transitively pulls DB deps | `importlinter` catches this in CI. Pre-push, run `uv run importlinter` locally. |
| Platform imports break because `zeroth.core.graph` → `zeroth.core.graph` rename missed a file | Automated codemod pass using ruff's import rewriter or a small sed script; followed by `uv run pytest` locally to confirm. |
| PyPI name `zeroth-core` already taken | Check pypi.org before naming the GitHub repo. If taken, fall back to `zeroth-sdk` and update the design. |
| In-flight worktree work gets stranded | Step 0 audits and commits or discards all worktree state before the split begins. User confirms clean `git status` before I proceed. |
| Planning doc rewrites lose context | Keep full archive repo intact on disk and on GitHub (read-only). Rewrites are additive/renaming, not lossy — original paths always recoverable from the archive. |
| PyPI trusted publisher setup requires manual user action | Documented as an explicit pre-release step. CI workflow ready to go once the publisher is configured. |
| GovernAI dep is a git URL, not PyPI — may confuse `pip install zeroth-core` users | Document clearly in core README. Accept as a known limitation for 0.1.0; upstream `governai` PyPI publish is out of scope. |
| Local `econ-instrumentation-sdk @ file:///.../regulus/sdk/python` path dep breaks CI for platform | Addressed by Step 6.5: publish `econ-instrumentation-sdk` to PyPI (name already confirmed available), then switch platform dep. |
| Regulus repo not yet on GitHub, or SDK history entangled with other regulus packages | Regulus publish paused at gate until user confirms source repo. Worst case: we publish 0.1.1 from a fresh commit without full SDK history — version can always be re-released later with proper history. |
| Worktree sweep commits partial/broken state that leaks into branches someone will later pick up | The wip commits are clearly labeled `wip: archive snapshot before core/platform split`. They live on the worktree branches only, in the archive repo. No new repo starts from any worktree branch — all new repos start from the current `main` state. |

## Execution gates

Before I take any destructive or irreversible action, I will stop and confirm with the user at each of these gates:

1. **Before Step 0 (worktree audit):** confirm which worktrees can be committed vs. discarded.
2. **Before Step 1 (archive):** confirm "move `/Users/dondoe/coding/zeroth/` to `/Users/dondoe/coding/zeroth-archive/`" is authorized. Confirm whether a GitHub `rrrozhd/zeroth` repo exists and should be renamed + archived.
3. **Before Step 2 (`gh repo create`):** confirm the final names `zeroth-core` and `zeroth-platform` are free on both GitHub and PyPI.
4. **Before Step 4 (push to GitHub):** confirm the filter-repo output trees look correct.
5. **Before deleting or force-pushing anything that exists remotely:** always confirm.

No other action requires explicit confirmation — file moves within `/tmp/` clones, local edits, building the module split, writing planning doc updates all proceed autonomously under the umbrella of the overall approval of this design.

## Deliverables

When the split is complete:

1. `github.com/rrrozhd/zeroth-archive` — read-only, full history preserved, all 36 worktree branches snapshotted with `wip` commits.
2. `github.com/rrrozhd/zeroth-core` — CI green, pip-installable from git, scoped planning docs. Tag `v0.1.0` after TestPyPI + PyPI verification. Published on PyPI as `zeroth-core`.
3. `github.com/rrrozhd/zeroth-platform` — CI green (tests + lint + studio build), depends on `zeroth-core` + `econ-instrumentation-sdk` from PyPI, full planning state moved over with paths rewritten.
4. `econ-instrumentation-sdk` `0.1.1` published to PyPI from the regulus repo.
5. Local `/Users/dondoe/coding/zeroth/` containing both subrepos.
6. Local `/Users/dondoe/coding/zeroth-archive/` intact, with all worktrees reachable.
7. User writes a new app against `zeroth-core` — unblocked.

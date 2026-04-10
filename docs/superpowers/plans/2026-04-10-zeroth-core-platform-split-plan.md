# Zeroth Core/Platform Split — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single `/Users/dondoe/coding/zeroth/` repo into two independent public GitHub repos (`rrrozhd/zeroth-core` SDK + `rrrozhd/zeroth-platform` service) while preserving all 36 in-flight worktrees, full git history, and publishing the Regulus econ-instrumentation SDK to PyPI as a side dependency.

**Architecture:** Multi-layer archive first (tarball → bare mirror → pushed branches → GitHub archive flag), then `git filter-repo` two-pass split into new repos under a PEP 420 namespace package `zeroth.core.*` / `zeroth.platform.*`. Core ships repository `Protocol`s + in-memory implementations; platform ships SQL-backed ones. Enforced by `importlinter` in CI.

**Spec:** `docs/superpowers/specs/2026-04-10-zeroth-core-platform-split-design.md`

**Tech Stack:** Python 3.12+, uv, pytest, ruff, importlinter, hatchling, git filter-repo, GitHub Actions, PyPI trusted publishing (OIDC), setuptools (regulus only), gh CLI.

## Gate semantics

Steps marked **GATE** STOP execution and require explicit user "proceed" before continuing. These are the only points where human intervention is mandatory. Between gates, the executor runs autonomously.

**Gates in this plan:**
- G1: After Layer 1 tarball — user confirms tarball exists & non-empty
- G2: After filter-repo dry runs — user inspects trees before push
- G3: Before `gh repo create` — user confirms names and visibility
- G4: Before `git push` to new public remotes — user confirms
- G5: Before GitHub archive rename on old repo — user confirms old repo name exists
- G6: Before regulus `gh repo create` — user confirms content is clean
- G7: Before PyPI prod publish (core) — user confirms TestPyPI worked
- G8: Before PyPI prod publish (regulus) — user confirms TestPyPI worked
- G9: Before swapping platform dep from git to PyPI — user confirms installs work

## File structure overview

### Current (source of truth)
```
/Users/dondoe/coding/zeroth/
├── src/zeroth/{graph,orchestrator,runs,...}/
├── apps/studio/
├── tests/
├── docs/, phases/, .planning/, PROGRESS.md, PLAN.md, CLAUDE.md
└── .claude/worktrees/agent-*/   (36 of them)
```

### After this plan
```
/Users/dondoe/coding/
├── zeroth-archive/                              # local archive (renamed from zeroth/)
├── zeroth-snapshot-<ts>.tar.gz                  # Layer 1 preservation artifact
├── zeroth-snapshot-<ts>.sha256
├── zeroth-mirror.git/                           # Layer 2 bare mirror
├── regulus-snapshot-<ts>.tar.gz                 # regulus preservation
├── regulus/                                     # existing, now a git repo + GH remote
└── zeroth/                                      # NEW parent dir (not a repo)
    ├── zeroth-core/                             # cloned from rrrozhd/zeroth-core
    │   ├── pyproject.toml
    │   ├── src/zeroth/core/...
    │   ├── tests/
    │   ├── docs/, phases/, PROGRESS.md, CLAUDE.md, README.md
    │   └── .github/workflows/{ci.yml,publish.yml}
    └── zeroth-platform/                         # cloned from rrrozhd/zeroth-platform
        ├── pyproject.toml
        ├── src/zeroth/platform/...
        ├── apps/studio/
        ├── tests/
        ├── docs/, phases/, .planning/, PROGRESS.md, CLAUDE.md, README.md
        └── .github/workflows/ci.yml
```

---

## Phase 0 — Preservation (the safety-critical phase)

Before anything is touched, four independent preservation layers are created. Every step in this phase is non-destructive and reversible.

### Task 0.1: Anchor the detached HEAD

The current repo is in detached HEAD state — if we do nothing, our recent commits (including this plan) risk garbage collection later. Anchor them first.

**Files:** none created, one branch created

- [ ] **Step 1:** Verify detached state and note commit
  ```bash
  cd /Users/dondoe/coding/zeroth
  git status | head -3
  git rev-parse HEAD
  ```
  Expected: "HEAD detached from ..." and a SHA.

- [ ] **Step 2:** Create branch at current HEAD
  ```bash
  git branch pre-split-head
  git log --oneline pre-split-head -5
  ```
  Expected: branch created, log shows the design doc + plan commits.

- [ ] **Step 3:** Commit this plan document (it's still uncommitted)
  ```bash
  git add docs/superpowers/plans/2026-04-10-zeroth-core-platform-split-plan.md
  git commit -m "docs: core/platform split implementation plan

  Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
  git branch -f pre-split-head HEAD
  ```

### Task 0.2: Layer 1 — Filesystem tarball

**Files:**
- Create: `/Users/dondoe/coding/zeroth-snapshot-<timestamp>.tar.gz`
- Create: `/Users/dondoe/coding/zeroth-snapshot-<timestamp>.sha256`

- [ ] **Step 1:** Create the tarball (excluding rebuildable noise)
  ```bash
  cd /Users/dondoe/coding
  TS=$(date +%Y%m%d-%H%M%S)
  tar --exclude='zeroth/node_modules' \
      --exclude='zeroth/apps/studio/node_modules' \
      --exclude='zeroth/.venv' \
      --exclude='zeroth/**/__pycache__' \
      --exclude='zeroth/**/*.pyc' \
      -czf "zeroth-snapshot-${TS}.tar.gz" zeroth/
  echo "SNAPSHOT_TS=$TS" > /tmp/zeroth-split-state
  ```

- [ ] **Step 2:** Record checksum + size
  ```bash
  cd /Users/dondoe/coding
  TS=$(grep SNAPSHOT_TS /tmp/zeroth-split-state | cut -d= -f2)
  shasum -a 256 "zeroth-snapshot-${TS}.tar.gz" > "zeroth-snapshot-${TS}.sha256"
  ls -lh "zeroth-snapshot-${TS}.tar.gz"
  cat "zeroth-snapshot-${TS}.sha256"
  ```
  Expected: file > 100 MB, sha256 recorded.

- [ ] **Step 3 (GATE G1):** STOP. Print tarball path + size + sha256 and wait for user "proceed".

### Task 0.3: Layer 2 — Bare mirror clone

**Files:**
- Create: `/Users/dondoe/coding/zeroth-mirror.git/`

- [ ] **Step 1:** Clone as mirror (captures all refs + reflog)
  ```bash
  cd /Users/dondoe/coding
  git clone --mirror zeroth zeroth-mirror.git
  ```

- [ ] **Step 2:** Verify ref count and commit count match source
  ```bash
  cd /Users/dondoe/coding/zeroth
  SRC_BRANCHES=$(git branch -a | wc -l)
  SRC_COMMITS=$(git log --all --oneline | wc -l)
  cd /Users/dondoe/coding/zeroth-mirror.git
  MIR_BRANCHES=$(git branch -a | wc -l)
  MIR_COMMITS=$(git log --all --oneline | wc -l)
  echo "src branches: $SRC_BRANCHES / mirror branches: $MIR_BRANCHES"
  echo "src commits: $SRC_COMMITS / mirror commits: $MIR_COMMITS"
  test "$SRC_COMMITS" = "$MIR_COMMITS" && echo "OK" || echo "MISMATCH"
  ```
  Expected: OK. If MISMATCH, STOP and investigate.

### Task 0.4: Layer 3 — Commit in-flight work across all worktrees

**Files:**
- Modify: `.gitignore` (add DB files)
- Create: many commits across worktree branches and main working tree

- [ ] **Step 1:** Ensure local DBs are gitignored
  ```bash
  cd /Users/dondoe/coding/zeroth
  grep -q '^zeroth.db$' .gitignore || echo 'zeroth.db' >> .gitignore
  grep -q '^zeroth_dev.db$' .gitignore || echo 'zeroth_dev.db' >> .gitignore
  grep -q '^zeroth_live.db$' .gitignore || echo 'zeroth_live.db' >> .gitignore
  git diff .gitignore
  ```

- [ ] **Step 2:** Worktree sweep (scripted, idempotent)
  ```bash
  cd /Users/dondoe/coding/zeroth
  for wt in $(git worktree list --porcelain | awk '/^worktree /{print $2}' | grep worktrees/agent-); do
    (
      cd "$wt"
      if [ -n "$(git status --porcelain)" ]; then
        echo "--- committing wip in $wt ---"
        git add -A
        git commit -m "wip: archive snapshot before core/platform split" || true
      fi
    )
  done
  ```

- [ ] **Step 3:** Main tree sweep — commit everything non-DB
  ```bash
  cd /Users/dondoe/coding/zeroth
  git add -A
  git status | head -20
  git commit -m "wip: archive snapshot before core/platform split" || true
  ```

- [ ] **Step 4:** Verify clean state
  ```bash
  cd /Users/dondoe/coding/zeroth
  git status
  git worktree list | wc -l
  ```
  Expected: clean working tree, 36+ worktrees still listed.

### Task 0.5: Layer 3 continued — Push everything to current origin

- [ ] **Step 1:** Check if origin exists and is reachable
  ```bash
  cd /Users/dondoe/coding/zeroth
  git remote -v
  git ls-remote origin 2>&1 | head -3 || echo "NO REMOTE or UNREACHABLE"
  ```

- [ ] **Step 2:** If origin exists, push all branches + tags
  ```bash
  cd /Users/dondoe/coding/zeroth
  git push origin --all 2>&1 | tail -20
  git push origin --tags 2>&1 | tail -5
  ```
  If no origin: skip this step, record in `/tmp/zeroth-split-state` as `HAD_REMOTE=false`.

- [ ] **Step 3:** Verify `pre-split-head` branch is pushed (if remote exists)
  ```bash
  cd /Users/dondoe/coding/zeroth
  git push origin pre-split-head
  ```

### Task 0.6: Layer 4 — Archive the GitHub remote (if it exists)

- [ ] **Step 1 (GATE G5):** STOP. Ask user: does `rrrozhd/zeroth` exist on GitHub? Is it the correct remote to archive?

- [ ] **Step 2:** If yes, rename and archive
  ```bash
  gh api -X PATCH repos/rrrozhd/zeroth -f name=zeroth-archive
  gh api -X PATCH repos/rrrozhd/zeroth-archive -F archived=true
  gh repo view rrrozhd/zeroth-archive --json isArchived,name
  ```
  Expected: `{"isArchived": true, "name": "zeroth-archive"}`.

- [ ] **Step 3:** Update local remote URL to point at the renamed remote
  ```bash
  cd /Users/dondoe/coding/zeroth
  git remote set-url origin git@github.com:rrrozhd/zeroth-archive.git
  git ls-remote origin 2>&1 | head -3
  ```

---

## Phase 1 — Build `zeroth-core` in `/tmp/`

We build the new repo contents in a scratch directory first, using a clone of the source repo. Nothing in `/Users/dondoe/coding/zeroth/` is touched.

### Task 1.1: Fresh scratch clone

- [ ] **Step 1:** Clone source into scratch
  ```bash
  mkdir -p /tmp/zeroth-split
  cd /tmp/zeroth-split
  rm -rf zeroth-core-build
  git clone /Users/dondoe/coding/zeroth zeroth-core-build
  cd zeroth-core-build
  git checkout pre-split-head
  git log --oneline -3
  ```

### Task 1.2: Install `git-filter-repo`

- [ ] **Step 1:** Verify or install
  ```bash
  which git-filter-repo || pip install git-filter-repo || brew install git-filter-repo
  git filter-repo --version
  ```

### Task 1.3: First filter-repo pass — keep core paths, rename into `src/zeroth/core/`

**Files:**
- This step rewrites all history of the scratch clone

- [ ] **Step 1:** Run filter-repo with explicit path allow-list and rename rule
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  git filter-repo --force \
    --path src/zeroth/graph/ \
    --path src/zeroth/orchestrator/ \
    --path src/zeroth/contracts/ \
    --path src/zeroth/runs/__init__.py \
    --path src/zeroth/runs/models.py \
    --path src/zeroth/execution_units/ \
    --path src/zeroth/agent_runtime/ \
    --path src/zeroth/mappings/ \
    --path src/zeroth/conditions/ \
    --path src/zeroth/approvals/ \
    --path src/zeroth/audit/ \
    --path src/zeroth/policy/ \
    --path src/zeroth/secrets/ \
    --path src/zeroth/memory/ \
    --path src/zeroth/guardrails/ \
    --path tests/ \
    --path docs/ \
    --path README.md \
    --path LICENSE \
    --path-rename src/zeroth/:src/zeroth/core/
  ```

- [ ] **Step 2:** Inspect the result tree
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  find src -type d | head -20
  find src -name "*.py" | wc -l
  git log --oneline | head -5
  ```

### Task 1.4: Remove platform files that slipped in (repository.py, thread_store.py, etc.)

`filter-repo --path src/zeroth/runs/` would keep everything under `runs/`, but we only wanted `runs/__init__.py` + `runs/models.py`. Same for `agent_runtime/thread_store.py`, `approvals/repository.py`, `audit/repository.py`, `audit/verifier.py`, `memory/pgvector_connector.py`, `guardrails/rate_limit.py`. Because we explicitly listed only the wanted files above for those modules, they should already be excluded. Verify.

- [ ] **Step 1:** Verify unwanted files are absent
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  test ! -f src/zeroth/core/runs/repository.py && echo "OK: runs/repository.py removed" || echo "FAIL"
  test ! -f src/zeroth/core/agent_runtime/thread_store.py && echo "OK: thread_store.py removed" || echo "FAIL"
  test ! -f src/zeroth/core/approvals/repository.py && echo "OK: approvals/repository.py removed" || echo "FAIL"
  test ! -f src/zeroth/core/audit/repository.py && echo "OK: audit/repository.py removed" || echo "FAIL"
  test ! -f src/zeroth/core/audit/verifier.py && echo "OK: audit/verifier.py removed" || echo "FAIL"
  test ! -f src/zeroth/core/memory/pgvector_connector.py && echo "OK: pgvector removed" || echo "FAIL"
  test ! -f src/zeroth/core/guardrails/rate_limit.py && echo "OK: rate_limit removed" || echo "FAIL"
  ```
  If any FAIL: add the offending paths to `filter-repo --invert-paths` in a follow-up pass and re-verify.

- [ ] **Step 2:** If any slipped through, do a cleanup filter-repo
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  git filter-repo --force --invert-paths \
    --path src/zeroth/core/runs/repository.py \
    --path src/zeroth/core/agent_runtime/thread_store.py \
    --path src/zeroth/core/approvals/repository.py \
    --path src/zeroth/core/audit/repository.py \
    --path src/zeroth/core/audit/verifier.py \
    --path src/zeroth/core/memory/pgvector_connector.py \
    --path src/zeroth/core/guardrails/rate_limit.py || true
  # re-verify from step 1
  ```

### Task 1.5: Delete the old `src/zeroth/__init__.py`

The existing `src/zeroth/__init__.py` re-exports from `zeroth.core.storage`, which won't exist in core and pollutes the namespace package. Delete it.

- [ ] **Step 1:** Delete and commit
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  rm -f src/zeroth/__init__.py
  git add -A
  git commit -m "chore: drop top-level zeroth/__init__.py for PEP 420 namespace"
  ```

### Task 1.6: Write new `pyproject.toml` for `zeroth-core`

**Files:**
- Modify: `/tmp/zeroth-split/zeroth-core-build/pyproject.toml` (replace entirely)

- [ ] **Step 1:** Write minimal core pyproject
  ```toml
  [project]
  name = "zeroth-core"
  version = "0.1.0"
  description = "Zeroth SDK: governed multi-agent graphs as a Python library"
  readme = "README.md"
  requires-python = ">=3.12"
  license = { text = "MIT" }
  authors = [{ name = "rrrozhd" }]
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

  [project.urls]
  Homepage = "https://github.com/rrrozhd/zeroth-core"
  Repository = "https://github.com/rrrozhd/zeroth-core"

  [dependency-groups]
  dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "ruff>=0.7",
    "import-linter>=2",
    "mypy>=1.13",
  ]

  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [tool.hatch.build.targets.wheel]
  packages = ["src/zeroth"]

  [tool.ruff]
  target-version = "py312"
  line-length = 100

  [tool.ruff.lint]
  select = ["E", "F", "I", "N", "W", "UP", "B", "SIM", "TCH"]

  [tool.pytest.ini_options]
  asyncio_mode = "auto"
  testpaths = ["tests"]

  [tool.importlinter]
  root_package = "zeroth"

  [[tool.importlinter.contracts]]
  name = "core does not depend on platform or IO stacks"
  type = "forbidden"
  source_modules = ["zeroth.core"]
  forbidden_modules = [
    "zeroth.platform",
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "alembic",
    "redis",
    "arq",
    "chromadb",
    "elasticsearch",
    "pgvector",
  ]
  ```

- [ ] **Step 2:** Commit
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  git add pyproject.toml
  git commit -m "chore(core): new pyproject.toml for zeroth-core distribution"
  ```

### Task 1.7: Add repository protocols and in-memory implementations

Core needs `Protocol` types for every repository it imports, plus in-memory implementations. We take them one at a time, TDD-style.

#### Task 1.7.1: GraphRepository protocol + in-memory

**Files:**
- Create: `src/zeroth/core/graph/protocols.py`
- Create: `src/zeroth/core/graph/memory_repository.py`
- Create: `tests/core/graph/test_memory_repository.py`

- [ ] **Step 1:** Write the failing test
  ```python
  # tests/core/graph/test_memory_repository.py
  import pytest
  from zeroth.core.graph import Graph, GraphStatus
  from zeroth.core.graph.memory_repository import InMemoryGraphRepository


  @pytest.fixture
  def sample_graph() -> Graph:
      return Graph(
          graph_id="g1",
          name="g1",
          version=1,
          status=GraphStatus.DRAFT,
          nodes=[],
          edges=[],
          settings={},
      )


  async def test_save_and_get(sample_graph: Graph) -> None:
      repo = InMemoryGraphRepository()
      await repo.save(sample_graph)
      loaded = await repo.get("g1", 1)
      assert loaded == sample_graph


  async def test_get_latest_version(sample_graph: Graph) -> None:
      repo = InMemoryGraphRepository()
      await repo.save(sample_graph)
      v2 = sample_graph.model_copy(update={"version": 2})
      await repo.save(v2)
      latest = await repo.get("g1")
      assert latest.version == 2


  async def test_list(sample_graph: Graph) -> None:
      repo = InMemoryGraphRepository()
      await repo.save(sample_graph)
      results = list(await repo.list())
      assert len(results) == 1
  ```

- [ ] **Step 2:** Run — expect import error
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  uv run pytest tests/core/graph/test_memory_repository.py -v 2>&1 | tail -20
  ```

- [ ] **Step 3:** Write the protocol
  ```python
  # src/zeroth/core/graph/protocols.py
  from __future__ import annotations
  from collections.abc import Sequence
  from typing import Protocol
  from zeroth.core.graph.models import Graph


  class GraphRepository(Protocol):
      """Abstract persistence for versioned graph documents."""

      async def save(self, graph: Graph) -> Graph: ...
      async def get(self, graph_id: str, version: int | None = None) -> Graph | None: ...
      async def list(self) -> Sequence[Graph]: ...
      async def publish(self, graph_id: str, version: int) -> Graph: ...
      async def archive(self, graph_id: str) -> None: ...
  ```

- [ ] **Step 4:** Write the in-memory implementation
  ```python
  # src/zeroth/core/graph/memory_repository.py
  from __future__ import annotations
  from collections.abc import Sequence
  from zeroth.core.graph.errors import GraphLifecycleError
  from zeroth.core.graph.models import Graph, GraphStatus


  class InMemoryGraphRepository:
      """Dict-backed graph repository. For tests, notebooks, embedded use."""

      def __init__(self) -> None:
          self._store: dict[tuple[str, int], Graph] = {}

      async def save(self, graph: Graph) -> Graph:
          key = (graph.graph_id, graph.version)
          existing = self._store.get(key)
          if existing and existing.status != GraphStatus.DRAFT:
              raise GraphLifecycleError(
                  f"graph version {graph.graph_id}@{graph.version} is immutable"
              )
          self._store[key] = graph
          return graph

      async def get(self, graph_id: str, version: int | None = None) -> Graph | None:
          matches = [g for (gid, _), g in self._store.items() if gid == graph_id]
          if not matches:
              return None
          if version is None:
              return max(matches, key=lambda g: g.version)
          for g in matches:
              if g.version == version:
                  return g
          return None

      async def list(self) -> Sequence[Graph]:
          return list(self._store.values())

      async def publish(self, graph_id: str, version: int) -> Graph:
          graph = await self.get(graph_id, version)
          if graph is None:
              raise GraphLifecycleError(f"{graph_id}@{version} not found")
          published = graph.model_copy(update={"status": GraphStatus.PUBLISHED})
          self._store[(graph_id, version)] = published
          return published

      async def archive(self, graph_id: str) -> None:
          for key, graph in list(self._store.items()):
              if key[0] == graph_id:
                  self._store[key] = graph.model_copy(update={"status": GraphStatus.ARCHIVED})
  ```

- [ ] **Step 5:** Export from graph package
  ```python
  # append to src/zeroth/core/graph/__init__.py
  from zeroth.core.graph.memory_repository import InMemoryGraphRepository
  from zeroth.core.graph.protocols import GraphRepository
  ```
  (And add to `__all__`.)

- [ ] **Step 6:** Run tests — expect pass
  ```bash
  uv run pytest tests/core/graph/test_memory_repository.py -v
  ```

- [ ] **Step 7:** Commit
  ```bash
  git add src/zeroth/core/graph/protocols.py src/zeroth/core/graph/memory_repository.py \
          src/zeroth/core/graph/__init__.py tests/core/graph/test_memory_repository.py
  git commit -m "feat(core): add GraphRepository protocol and in-memory impl"
  ```

#### Task 1.7.2: RunRepository protocol + in-memory

**Files:**
- Create: `src/zeroth/core/runs/protocols.py`
- Create: `src/zeroth/core/runs/memory_repository.py`
- Create: `tests/core/runs/test_memory_repository.py`

- [ ] **Step 1-7:** Mirror Task 1.7.1 structure. Test the public methods used by `RuntimeOrchestrator`: `create`, `get`, `update_status`, `append_history`, `persist_condition_result`. Look at the current `src/zeroth/core/runs/repository.py` signatures (it was filtered out but exists in the `zeroth-archive`) to match the public interface.

- [ ] **Step 8:** Commit
  ```bash
  git commit -m "feat(core): add RunRepository protocol and in-memory impl"
  ```

#### Task 1.7.3: AuditRepository, ApprovalRepository, ThreadResolver, MemoryStore

- [ ] **Step 1:** Repeat the TDD cycle for each. Minimal interface to make `RuntimeOrchestrator` happy; integration tests are in platform.

- [ ] **Step 2:** Commit each separately with its own feat commit.

### Task 1.8: Fix all internal imports in core

The moved files still reference `from zeroth.core.graph import X`. They must become `from zeroth.core.graph import X`.

**Files:** every `.py` file under `src/zeroth/core/` and `tests/`

- [ ] **Step 1:** Automated codemod
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  # Replace zeroth.<module> with zeroth.core.<module> for core-only modules
  for mod in graph orchestrator contracts runs execution_units agent_runtime \
             mappings conditions approvals audit policy secrets memory guardrails; do
    find src tests -type f -name "*.py" -print0 | \
      xargs -0 sed -i '' -E "s|from zeroth\.${mod}|from zeroth.core.${mod}|g; s|import zeroth\.${mod}|import zeroth.core.${mod}|g"
  done
  git diff --stat | tail -5
  ```

- [ ] **Step 2:** Sanity check — no remaining `from zeroth.core.graph` / `from zeroth.core.orchestrator` etc.
  ```bash
  grep -rn "from zeroth\.graph\b\|from zeroth\.orchestrator\b\|from zeroth\.runs\b" src/ tests/ || echo "clean"
  ```

- [ ] **Step 3:** Commit
  ```bash
  git add -A
  git commit -m "refactor(core): rewrite imports to zeroth.core.*"
  ```

### Task 1.9: Purge tests that need DB/platform

Some tests under `tests/` were for DB-backed repos. They'll fail in core. Move them aside or delete.

- [ ] **Step 1:** Identify DB-dependent tests
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  grep -rln "AsyncDatabase\|PostgresDatabase\|SQLiteDatabase\|AsyncPostgres\|AsyncSQLite" tests/ || echo "none"
  grep -rln "from zeroth.core.storage\|from zeroth.core.storage" tests/ || echo "none"
  ```

- [ ] **Step 2:** Delete DB-dependent test files
  ```bash
  # one at a time, reviewing each
  # rm tests/.../test_graph_repository.py
  ```

- [ ] **Step 3:** Run the surviving test suite
  ```bash
  uv sync
  uv run pytest -v 2>&1 | tail -40
  ```
  Expected: all pass OR known failing tests identified. Fix import errors case-by-case.

- [ ] **Step 4:** Commit
  ```bash
  git add -A
  git commit -m "test(core): remove DB-dependent tests (moved to platform)"
  ```

### Task 1.10: Add new CLAUDE.md and README.md

**Files:**
- Modify: `CLAUDE.md` (replace)
- Modify: `README.md` (replace)

- [ ] **Step 1:** Write core CLAUDE.md
  ```markdown
  # Zeroth Core — Agent Guidelines

  ## Project

  zeroth-core is the SDK: a pure Python library for defining and executing governed multi-agent graphs. No FastAPI. No database. No web server. Users `pip install zeroth-core` and build apps on top.

  ## Hard rules

  - `zeroth.core.*` MUST NOT import `fastapi`, `sqlalchemy`, `psycopg`, `alembic`, `redis`, `arq`, `chromadb`, `elasticsearch`, `pgvector`, or `zeroth.platform.*`.
  - Enforced in CI by `importlinter`. Run `uv run lint-imports` before committing.
  - All repositories are `Protocol` types with in-memory default implementations. Concrete DB-backed implementations live in `zeroth-platform`.
  - Tests must pass with zero infrastructure (`uv run pytest` in a fresh clone).

  ## Build & test

  ```bash
  uv sync
  uv run pytest -v
  uv run ruff check src/ tests/
  uv run lint-imports
  uv build
  ```

  ## Layout

  ```
  src/zeroth/core/
  ├── graph/         # Graph models, validation, diff, versioning, in-memory repo
  ├── orchestrator/  # RuntimeOrchestrator (pure, DI'd)
  ├── contracts/     # Contract registry
  ├── runs/          # Run models + in-memory repo
  ├── agent_runtime/ # Agent runner abstraction
  ├── execution_units/
  ├── mappings/
  ├── conditions/
  ├── approvals/
  ├── audit/
  ├── policy/
  ├── secrets/
  ├── memory/
  └── guardrails/
  ```
  ```

- [ ] **Step 2:** Write core README.md
  ```markdown
  # zeroth-core

  Zeroth SDK: governed multi-agent graphs as a Python library.

  ## Install

  ```bash
  pip install zeroth-core
  ```

  ## Quickstart

  ```python
  from zeroth.core.graph import Graph, AgentNode
  from zeroth.core.orchestrator import RuntimeOrchestrator
  from zeroth.core.runs import InMemoryRunRepository
  # ... build your graph and execute ...
  ```

  See `docs/` for the full guide.

  ## Related

  - `zeroth-platform` — the full governed multi-agent service built on top of `zeroth-core`.
  ```

- [ ] **Step 3:** Commit
  ```bash
  git add CLAUDE.md README.md
  git commit -m "docs(core): add scoped CLAUDE.md and README.md"
  ```

### Task 1.11: Add `.github/workflows/ci.yml` and `publish.yml`

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/publish.yml`

- [ ] **Step 1:** Write `ci.yml` (content from the design doc Section 5)

- [ ] **Step 2:** Write `publish.yml` (trusted publishing OIDC)

- [ ] **Step 3:** Commit
  ```bash
  git add .github/
  git commit -m "ci(core): add test/lint/publish workflows"
  ```

### Task 1.12: Local verification for core

- [ ] **Step 1:** Full local pass
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  uv sync --dev
  uv run pytest -v
  uv run ruff check src/ tests/
  uv run ruff format --check src/ tests/
  uv run lint-imports
  uv build
  ls -lh dist/
  ```
  Expected: all green, `dist/zeroth_core-0.1.0-py3-none-any.whl` exists.

- [ ] **Step 2:** Fresh-venv smoke test
  ```bash
  cd /tmp
  python -m venv zeroth-core-smoke
  source zeroth-core-smoke/bin/activate
  pip install /tmp/zeroth-split/zeroth-core-build/dist/zeroth_core-0.1.0-py3-none-any.whl
  python -c "from zeroth.core.graph import Graph; from zeroth.core.orchestrator import RuntimeOrchestrator; print('ok')"
  deactivate
  ```

- [ ] **Step 3 (GATE G2):** STOP. Print tree stats, verification results. Wait for user "proceed".

---

## Phase 2 — Build `zeroth-platform` in `/tmp/`

### Task 2.1: Fresh scratch clone for platform

- [ ] **Step 1:**
  ```bash
  cd /tmp/zeroth-split
  rm -rf zeroth-platform-build
  git clone /Users/dondoe/coding/zeroth zeroth-platform-build
  cd zeroth-platform-build
  git checkout pre-split-head
  ```

### Task 2.2: Filter-repo pass B — drop pure core files

- [ ] **Step 1:** Build the invert-path list — every file that went to core-exclusive (graph/models, orchestrator, contracts, etc.)
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  git filter-repo --force --invert-paths \
    --path src/zeroth/graph/models.py \
    --path src/zeroth/graph/validation.py \
    --path src/zeroth/graph/diff.py \
    --path src/zeroth/graph/serialization.py \
    --path src/zeroth/graph/versioning.py \
    --path src/zeroth/graph/errors.py \
    --path src/zeroth/graph/storage.py \
    --path src/zeroth/graph/validation_errors.py \
    --path src/zeroth/orchestrator/ \
    --path src/zeroth/contracts/ \
    --path src/zeroth/runs/__init__.py \
    --path src/zeroth/runs/models.py \
    --path src/zeroth/execution_units/ \
    --path src/zeroth/agent_runtime/runner.py \
    --path src/zeroth/agent_runtime/__init__.py \
    --path src/zeroth/mappings/ \
    --path src/zeroth/conditions/ \
    --path src/zeroth/approvals/models.py \
    --path src/zeroth/approvals/service.py \
    --path src/zeroth/approvals/__init__.py \
    --path src/zeroth/audit/models.py \
    --path src/zeroth/audit/__init__.py \
    --path src/zeroth/policy/ \
    --path src/zeroth/secrets/ \
    --path src/zeroth/memory/__init__.py \
    --path src/zeroth/guardrails/__init__.py
  ```
  Note: the exact file list must match the core allow-list exactly or risk double-keeping files. Cross-check with Task 1.3.

### Task 2.3: Rename `src/zeroth/*` to `src/zeroth/platform/*`

- [ ] **Step 1:**
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  git filter-repo --force --path-rename src/zeroth/:src/zeroth/platform/
  ```

- [ ] **Step 2:** Inspect
  ```bash
  find src -type d | head -20
  ```

### Task 2.4: Delete old top-level `__init__.py` (if it survived the rename)

- [ ] **Step 1:**
  ```bash
  rm -f src/zeroth/__init__.py
  git add -A && git commit -m "chore: drop top-level zeroth/__init__.py" || true
  ```

### Task 2.5: Codemod imports `zeroth.X` → `zeroth.core.X` for core modules, `zeroth.platform.X` for platform modules

- [ ] **Step 1:** Rewrite core-module imports
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  for mod in graph orchestrator contracts runs execution_units agent_runtime \
             mappings conditions approvals audit policy secrets memory guardrails; do
    find src tests -type f -name "*.py" -print0 | \
      xargs -0 sed -i '' -E "s|from zeroth\.${mod}|from zeroth.core.${mod}|g; s|import zeroth\.${mod}|import zeroth.core.${mod}|g"
  done
  ```
  **Warning:** this will incorrectly rewrite platform-side uses of `zeroth.core.graph.repository` etc. Those are already platform-side — we need to rewrite them back. Do step 2.

- [ ] **Step 2:** Rewrite platform-side module imports
  ```bash
  for mod in storage migrations service studio studio_api admin_api dispatch webhooks \
             observability deployments identity econ sandbox_sidecar config; do
    find src tests -type f -name "*.py" -print0 | \
      xargs -0 sed -i '' -E "s|from zeroth\.${mod}|from zeroth.platform.${mod}|g; s|import zeroth\.${mod}|import zeroth.platform.${mod}|g"
  done
  ```

- [ ] **Step 3:** Handle the split modules (graph, runs, audit, approvals, memory, guardrails, agent_runtime) where SOME submodules live in core (models, validation) and OTHERS in platform (repository, thread_store). The sed above already rewrote them all to `zeroth.core.*`. Fix the platform-only submodules:
  ```bash
  # Examples — do each mapping:
  sed -i '' 's|from zeroth.core.graph.repository|from zeroth.platform.repositories.graph_repository|g' src tests -r
  sed -i '' 's|from zeroth.core.runs.repository|from zeroth.platform.repositories.run_repository|g' src tests -r
  sed -i '' 's|from zeroth.core.audit.repository|from zeroth.platform.repositories.audit_repository|g' src tests -r
  sed -i '' 's|from zeroth.core.audit.verifier|from zeroth.platform.audit.verifier|g' src tests -r
  sed -i '' 's|from zeroth.core.approvals.repository|from zeroth.platform.repositories.approval_repository|g' src tests -r
  sed -i '' 's|from zeroth.core.agent_runtime.thread_store|from zeroth.platform.agent_runtime.thread_store|g' src tests -r
  sed -i '' 's|from zeroth.core.memory.pgvector_connector|from zeroth.platform.memory.pgvector_connector|g' src tests -r
  sed -i '' 's|from zeroth.core.guardrails.rate_limit|from zeroth.platform.guardrails.rate_limit|g' src tests -r
  ```

- [ ] **Step 4:** Move the platform-exclusive files into their new module homes
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  mkdir -p src/zeroth/platform/repositories
  git mv src/zeroth/platform/graph/repository.py src/zeroth/platform/repositories/graph_repository.py
  git mv src/zeroth/platform/runs/repository.py src/zeroth/platform/repositories/run_repository.py
  git mv src/zeroth/platform/audit/repository.py src/zeroth/platform/repositories/audit_repository.py
  git mv src/zeroth/platform/approvals/repository.py src/zeroth/platform/repositories/approval_repository.py
  # leave thread_store under agent_runtime, pgvector under memory, rate_limit under guardrails
  ```

- [ ] **Step 5:** Add `src/zeroth/platform/repositories/__init__.py` exporting the classes.

- [ ] **Step 6:** Commit
  ```bash
  git add -A
  git commit -m "refactor(platform): rewrite imports to zeroth.core / zeroth.platform split"
  ```

### Task 2.6: Write platform `pyproject.toml`

- [ ] **Step 1:** Replace with platform content (from design doc), including `zeroth-core @ git+https://github.com/rrrozhd/zeroth-core@main` and `econ-instrumentation-sdk @ git+https://github.com/rrrozhd/regulus.git@main#subdirectory=sdk/python` (until regulus SDK hits PyPI — then swap to `econ-instrumentation-sdk >= 0.1.1`).

- [ ] **Step 2:** Commit
  ```bash
  git commit -am "chore(platform): new pyproject.toml depending on zeroth-core"
  ```

### Task 2.7: Update platform CLAUDE.md, README.md, path references in planning docs

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `PROGRESS.md`, `PLAN.md`, `phases/**/PLAN.md`, `.planning/STATE.md`

- [ ] **Step 1:** Update CLAUDE.md — note the core dep, path changes.

- [ ] **Step 2:** Global path rewrite in docs/planning files
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  find PROGRESS.md PLAN.md phases/ .planning/ docs/ -type f \( -name "*.md" -o -name "*.json" \) \
    -exec sed -i '' \
      -e 's|src/zeroth/graph/|src/zeroth/core/graph/|g' \
      -e 's|src/zeroth/orchestrator/|src/zeroth/core/orchestrator/|g' \
      -e 's|src/zeroth/contracts/|src/zeroth/core/contracts/|g' \
      -e 's|src/zeroth/service/|src/zeroth/platform/service/|g' \
      -e 's|src/zeroth/storage/|src/zeroth/platform/storage/|g' \
      -e 's|src/zeroth/migrations/|src/zeroth/platform/migrations/|g' \
      {} +
  # add the other modules analogously
  ```

- [ ] **Step 3:** Review the rewrites
  ```bash
  git diff --stat PROGRESS.md PLAN.md phases/ .planning/ | tail -20
  ```

- [ ] **Step 4:** Commit
  ```bash
  git add -A
  git commit -m "docs(platform): rewrite path references for core/platform split"
  ```

### Task 2.8: Add `.github/workflows/ci.yml` (no publish)

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1:** Write workflow (from design doc)

- [ ] **Step 2:** Commit
  ```bash
  git add .github/
  git commit -m "ci(platform): add test/lint workflow"
  ```

### Task 2.9: Local verification (limited — CI needs real infra)

- [ ] **Step 1:** Sync (will pull zeroth-core from its current scratch path via a temporary dep override)
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  # Temporary: point to local scratch core until we push to GH
  uv sync --extra dev
  ```

- [ ] **Step 2:** Ruff + importlinter
  ```bash
  uv run ruff check src/ tests/
  uv run lint-imports
  ```

- [ ] **Step 3:** Unit tests (those that don't need DB)
  ```bash
  uv run pytest tests/ -v -k "not integration and not db" 2>&1 | tail -40
  ```
  Expected: reasonable majority pass. Full test suite runs in CI with postgres/redis.

- [ ] **Step 4 (GATE G2):** STOP. Report status. Wait for user "proceed".

---

## Phase 3 — Push to GitHub

### Task 3.1: Create the new GitHub repos

- [ ] **Step 1 (GATE G3):** STOP. Confirm with user: `rrrozhd/zeroth-core` and `rrrozhd/zeroth-platform` as public repos.

- [ ] **Step 2:** Create
  ```bash
  gh repo create rrrozhd/zeroth-core --public \
    --description "Zeroth SDK: governed multi-agent graphs as a Python library"
  gh repo create rrrozhd/zeroth-platform --public \
    --description "Zeroth platform: service, storage, studio built on zeroth-core"
  ```

- [ ] **Step 3:** Verify
  ```bash
  gh repo view rrrozhd/zeroth-core --json name,visibility,isEmpty
  gh repo view rrrozhd/zeroth-platform --json name,visibility,isEmpty
  ```

### Task 3.2: Push core

- [ ] **Step 1 (GATE G4):** STOP. Confirm ready to push core.

- [ ] **Step 2:**
  ```bash
  cd /tmp/zeroth-split/zeroth-core-build
  git remote remove origin 2>/dev/null || true
  git remote add origin git@github.com:rrrozhd/zeroth-core.git
  git push -u origin HEAD:main
  ```

- [ ] **Step 3:** Verify
  ```bash
  gh repo view rrrozhd/zeroth-core --json defaultBranchRef,diskUsage
  ```

### Task 3.3: Push platform

- [ ] **Step 1:**
  ```bash
  cd /tmp/zeroth-split/zeroth-platform-build
  git remote remove origin 2>/dev/null || true
  git remote add origin git@github.com:rrrozhd/zeroth-platform.git
  git push -u origin HEAD:main
  ```

- [ ] **Step 2:** Verify
  ```bash
  gh repo view rrrozhd/zeroth-platform --json defaultBranchRef,diskUsage
  ```

### Task 3.4: Wait for CI to be green on both

- [ ] **Step 1:** Watch CI
  ```bash
  gh run watch --repo rrrozhd/zeroth-core
  gh run watch --repo rrrozhd/zeroth-platform
  ```
  Platform CI may fail if: (a) postgres/redis service containers missing steps, (b) tests reference paths that didn't get rewritten, (c) econ-instrumentation-sdk dep not resolvable until Phase 5. Track issues, fix in follow-up commits to each repo, iterate until green.

---

## Phase 4 — Rearrange local working copies

### Task 4.1: Move current repo to archive location

- [ ] **Step 1 (GATE G5):** STOP. Final warning — this is the local rename step. Confirm the Layer 1 tarball and Layer 2 mirror still exist and are healthy.
  ```bash
  ls -lh /Users/dondoe/coding/zeroth-snapshot-*.tar.gz
  ls /Users/dondoe/coding/zeroth-mirror.git/
  ```

- [ ] **Step 2:** Rename
  ```bash
  mv /Users/dondoe/coding/zeroth /Users/dondoe/coding/zeroth-archive
  ```

- [ ] **Step 3:** Verify worktrees still resolvable in archive
  ```bash
  cd /Users/dondoe/coding/zeroth-archive
  git worktree list | head -5
  ```

### Task 4.2: Create new parent dir and clone new repos

- [ ] **Step 1:**
  ```bash
  mkdir -p /Users/dondoe/coding/zeroth
  cd /Users/dondoe/coding/zeroth
  gh repo clone rrrozhd/zeroth-core
  gh repo clone rrrozhd/zeroth-platform
  ls
  ```

- [ ] **Step 2:** Verify both build & test
  ```bash
  cd /Users/dondoe/coding/zeroth/zeroth-core
  uv sync --dev
  uv run pytest -v 2>&1 | tail -10

  cd /Users/dondoe/coding/zeroth/zeroth-platform
  uv sync --dev
  # can't run full test suite without DB; just verify sync works
  ```

---

## Phase 5 — Publish regulus SDK

### Task 5.1: Preserve regulus

- [ ] **Step 1:** Tarball
  ```bash
  cd /Users/dondoe/coding
  TS=$(date +%Y%m%d-%H%M%S)
  tar --exclude='regulus/**/node_modules' \
      --exclude='regulus/**/__pycache__' \
      --exclude='regulus/**/.venv' \
      --exclude='regulus/sdk/python/build' \
      --exclude='regulus/sdk/python/dist' \
      -czf "regulus-snapshot-${TS}.tar.gz" regulus/
  ls -lh "regulus-snapshot-${TS}.tar.gz"
  ```

### Task 5.2: Git init regulus (if needed) & create GitHub repo

- [ ] **Step 1:** Check current git state
  ```bash
  cd /Users/dondoe/coding/regulus
  ls -la .git 2>&1 | head -3
  git status 2>&1 | head -3
  ```

- [ ] **Step 2:** Init or commit outstanding
  ```bash
  cd /Users/dondoe/coding/regulus
  if [ ! -d .git ]; then
    git init
    # review .gitignore BEFORE adding
    cat .gitignore 2>/dev/null || echo "no .gitignore"
    git add -A
    git status | head -30
  fi
  ```

- [ ] **Step 3 (GATE G6):** STOP. User reviews `git status` for regulus — ensures no secrets, credentials, or sensitive files are about to be committed to a public repo. Add to `.gitignore` as needed before proceeding.

- [ ] **Step 4:** Commit and create public GitHub repo
  ```bash
  cd /Users/dondoe/coding/regulus
  git commit -m "initial import" || true
  gh repo create rrrozhd/regulus --public \
    --description "Regulus: economic instrumentation SDK and stack" \
    --source . --push
  ```

### Task 5.3: Add publish workflow

**Files:**
- Create: `/Users/dondoe/coding/regulus/.github/workflows/publish-sdk.yml`

- [ ] **Step 1:** Write workflow that builds from `sdk/python/` on tag `sdk-v*.*.*`

- [ ] **Step 2:** Commit and push
  ```bash
  cd /Users/dondoe/coding/regulus
  git add .github/
  git commit -m "ci: add publish-sdk workflow for econ-instrumentation-sdk"
  git push
  ```

### Task 5.4: Configure PyPI trusted publisher (manual user step)

- [ ] **Step 1:** Print instructions for user, wait:
  > Go to pypi.org → account settings → publishing → add trusted publisher.
  > Project: `econ-instrumentation-sdk`. Owner: `rrrozhd`. Repo: `regulus`. Workflow: `publish-sdk.yml`. Environment: `pypi`.
  > Repeat on test.pypi.org if using TestPyPI first.

- [ ] **Step 2 (GATE G8):** STOP. Wait for user confirmation that PyPI trusted publisher is configured.

### Task 5.5: TestPyPI release

- [ ] **Step 1:** Tag
  ```bash
  cd /Users/dondoe/coding/regulus
  git tag sdk-v0.1.1-test1
  git push origin sdk-v0.1.1-test1
  gh run watch
  ```

- [ ] **Step 2:** Verify install from TestPyPI
  ```bash
  python -m venv /tmp/econ-smoke
  source /tmp/econ-smoke/bin/activate
  pip install --index-url https://test.pypi.org/simple/ econ-instrumentation-sdk
  python -c "import econ_instrumentation; print('ok')"
  deactivate
  ```

### Task 5.6: Prod PyPI release

- [ ] **Step 1 (GATE G8):** Confirm TestPyPI worked before proceeding.

- [ ] **Step 2:** Switch workflow env to prod, tag, push, watch
  ```bash
  git tag sdk-v0.1.1
  git push origin sdk-v0.1.1
  gh run watch
  ```

- [ ] **Step 3:** Verify
  ```bash
  pip install econ-instrumentation-sdk
  ```

### Task 5.7: Update platform dep

- [ ] **Step 1 (GATE G9):** Confirm regulus is on PyPI.

- [ ] **Step 2:** Switch platform pyproject
  ```bash
  cd /Users/dondoe/coding/zeroth/zeroth-platform
  # edit pyproject.toml: econ-instrumentation-sdk >= 0.1.1
  uv sync --dev
  uv run pytest -v 2>&1 | tail -10
  git commit -am "chore(platform): depend on econ-instrumentation-sdk from PyPI"
  git push
  ```

---

## Phase 6 — Publish zeroth-core to PyPI

### Task 6.1: Configure PyPI trusted publisher for core

- [ ] **Step 1:** Instructions to user (TestPyPI + PyPI), same pattern as regulus.

- [ ] **Step 2 (GATE G7):** STOP. Wait.

### Task 6.2: TestPyPI release

- [ ] **Step 1:** Tag + push
  ```bash
  cd /Users/dondoe/coding/zeroth/zeroth-core
  git tag v0.1.0-test1
  git push origin v0.1.0-test1
  gh run watch
  ```

- [ ] **Step 2:** Verify install
  ```bash
  python -m venv /tmp/zc-smoke
  source /tmp/zc-smoke/bin/activate
  pip install --index-url https://test.pypi.org/simple/ zeroth-core
  python -c "from zeroth.core.graph import Graph; print('ok')"
  deactivate
  ```

### Task 6.3: Prod PyPI release

- [ ] **Step 1 (GATE G7):** Confirm.

- [ ] **Step 2:** Tag `v0.1.0`, push, watch CI.

### Task 6.4: Update platform dep to use PyPI core

- [ ] **Step 1:** Edit platform pyproject: `zeroth-core >= 0.1.0` (drop git URL).

- [ ] **Step 2:** Commit + push platform.

---

## Phase 7 — Final verification

### Task 7.1: Embedded usage smoke test

- [ ] **Step 1:** Write a throwaway script outside either repo:
  ```python
  # /tmp/smoke.py
  import asyncio
  from zeroth.core.graph import Graph, GraphStatus
  from zeroth.core.graph.memory_repository import InMemoryGraphRepository

  async def main():
      repo = InMemoryGraphRepository()
      g = Graph(graph_id="s1", name="s1", version=1, status=GraphStatus.DRAFT,
                nodes=[], edges=[], settings={})
      await repo.save(g)
      loaded = await repo.get("s1", 1)
      assert loaded == g
      print("embedded usage works")

  asyncio.run(main())
  ```

- [ ] **Step 2:** Run in clean venv with `pip install zeroth-core`.

- [ ] **Step 3:** Verify output.

### Task 7.2: Final CI sweep

- [ ] **Step 1:**
  ```bash
  gh run list --repo rrrozhd/zeroth-core --limit 3
  gh run list --repo rrrozhd/zeroth-platform --limit 3
  gh run list --repo rrrozhd/regulus --limit 3
  ```

### Task 7.3: Archive the local scratch builds

- [ ] **Step 1:**
  ```bash
  rm -rf /tmp/zeroth-split
  rm -rf /tmp/zc-smoke /tmp/zeroth-core-smoke /tmp/econ-smoke
  ```

### Task 7.4: Record completion in archive

- [ ] **Step 1:** Write a `SPLIT-COMPLETED.md` in `zeroth-archive/` root describing the split, the new repo URLs, the snapshot paths, and any known issues for future reference.

- [ ] **Step 2:** `cd /Users/dondoe/coding/zeroth-archive && git add SPLIT-COMPLETED.md && git commit -m "docs: split to zeroth-core + zeroth-platform completed"`. This commit lives only locally and only in the archive.

---

## Known risks during execution

| Risk | Mitigation |
|---|---|
| filter-repo path lists diverge between Task 1.3 and Task 2.2, causing files to appear in both or neither | Single source of truth: extract the path list into a shell variable or small text file reused by both passes |
| Codemod regex rewrites too aggressively | Run with `git diff --stat` review before commit; add `--` to grep boundaries |
| `zeroth.core.*` tests accidentally import platform module | importlinter catches in CI; also run locally before push |
| Regulus repo has secrets in .env or notebooks | GATE G6 forces manual `git status` review before first commit |
| PyPI trusted publisher setup fails | Documented in workflow; retry is safe |
| GH Actions runner has network issues | Retry job |
| `econ-instrumentation-sdk` breaking change between 0.1.1 expected API and what platform uses | Run platform full test suite after Task 5.7; if breaking, pin `==0.1.1` |

## Success criteria

- `gh repo view rrrozhd/zeroth-core` shows a public repo, CI green, `v0.1.0` tag.
- `pip install zeroth-core` works in a fresh venv, imports succeed, no IO-stack deps pulled in.
- `gh repo view rrrozhd/zeroth-platform` shows a public repo, CI green (including postgres/redis service).
- `gh repo view rrrozhd/zeroth-archive` shows `isArchived: true`.
- `gh repo view rrrozhd/regulus` shows a public repo with `econ-instrumentation-sdk 0.1.1` published.
- `/Users/dondoe/coding/zeroth/` contains `zeroth-core/` and `zeroth-platform/` as working clones.
- `/Users/dondoe/coding/zeroth-archive/` intact, `git worktree list` shows 36 worktrees.
- `/Users/dondoe/coding/zeroth-snapshot-*.tar.gz` exists and has recorded sha256.
- `/Users/dondoe/coding/zeroth-mirror.git/` exists as a bare mirror.
- User writes a new app against `zeroth-core`, unblocked.

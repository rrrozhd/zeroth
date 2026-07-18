# Backend Refactor Baseline

Captured before the backend architecture refactor on 2026-07-18 at
`2026-07-18T11:04:07-04:00`.

## Environment

- Worktree: `/Users/dondoe/coding/zeroth/.worktrees/backend-architecture-refactor`
- Branch: `codex/backend-architecture-refactor`
- Starting commit: `24e329a859ecd9e4a0deffef7ffe95073f3c714f`
- Platform: `Darwin Mac.school.local 25.5.0 Darwin Kernel Version 25.5.0: Mon Apr 27 20:41:19 PDT 2026; root:xnu-12377.121.6~2/RELEASE_ARM64_T8122 arm64`
- uv: `uv 0.11.3 (45da18ac3 2026-04-01 aarch64-apple-darwin)`
- Python: `Python 3.12.12`

## Dependency synchronization

Command:

```console
uv sync --all-extras
```

Result: exit code 0.

```text
Resolved 154 packages in 4ms
Checked 150 packages in 20ms
```

## Test baseline

Command:

```console
uv run pytest -q
```

Result: exit code 0.

```text
1973 passed, 16 deselected, 3 warnings in 92.87s (0:01:32)
```

The three warnings were:

1. `tests/execution_units/test_sandbox_strict_network.py::test_permissive_local_allows_network_bearing_node`
   emitted `UserWarning: local sandbox backend does not enforce network-access constraints`
   from `src/zeroth/core/execution_units/sandbox.py:516`.
2. `tests/service/test_admin_api.py::test_list_admin_runs_requires_auth`
   emitted `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead.`
   from `src/zeroth/econ_plane/main.py:60`.
3. `tests/service/test_admin_api.py::test_list_admin_runs_requires_auth`
   emitted the same `DeprecationWarning` from
   `.venv/lib/python3.12/site-packages/fastapi/applications.py:4599`.

## Optional connector import smoke test

Command:

```console
uv run python -c "import chromadb, elasticsearch, psycopg; from zeroth.core.memory.chroma_connector import ChromaDBMemoryConnector; from zeroth.core.memory.elastic_connector import ElasticsearchMemoryConnector; from zeroth.core.memory.pgvector_connector import PgvectorMemoryConnector"
```

Result: exit code 0 with no output. The ChromaDB, Elasticsearch, and pgvector
client packages and Zeroth connector classes all imported successfully.

## Frontend guard

Command:

```console
git diff --exit-code 01b36a9 -- frontend/
```

Result: exit code 0 with no output, confirming no frontend differences from the
designated baseline commit.

## Patch whitespace check

Command:

```console
git diff --check
```

Result: exit code 0 with no output.

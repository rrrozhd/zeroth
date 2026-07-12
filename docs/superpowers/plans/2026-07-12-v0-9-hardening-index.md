# Zeroth v0.9 Hardening Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Coordinate six independently testable hardening plans into a verified v0.9.1 bug-fix release candidate.

**Architecture:** Execute plans in dependency order. Runtime isolation and tenant ownership can proceed independently; coordination primitives must precede retention semantics; MCP/Vault can proceed after runtime isolation; documentation/release truth runs after behavior is final.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, aiosqlite, psycopg3, Alembic, pytest, Next.js, TypeScript, Vitest, Ruff, uv/hatchling.

---

## Source specification

- `docs/superpowers/specs/2026-07-12-v0-9-hardening-design.md`

## Plans and dependency order

1. `2026-07-12-v0-9-runtime-isolation.md` — no dependency.
2. `2026-07-12-v0-9-tenant-deployments.md` — no dependency.
3. `2026-07-12-v0-9-database-coordination.md` — no dependency; provides migration 010 and locking primitives.
4. `2026-07-12-v0-9-retention-correctness.md` — depends on plan 3.
5. `2026-07-12-v0-9-mcp-vault.md` — runner changes integrate after plan 1.
6. `2026-07-12-v0-9-product-release.md` — depends on final behavior from plans 1–5.

Migration ownership is fixed:

- `009_add_graph_workspace_scope.py` belongs to plan 2.
- `010_add_coordination_rows.py` belongs to plan 3.

## Integration checkpoints

- [ ] After plans 1–2: run `uv run pytest -q tests/agent_runtime tests/orchestrator tests/deployments tests/service/test_deployment_api.py tests/service/test_cross_tenant_leak_matrix.py`.
- [ ] After plans 3–4: run SQLite migration round-trip, durable audit-sequence skew/
  concurrency tests, and all `tests/audit tests/retention tests/storage`.
- [ ] After plan 5: run `tests/agent_runtime tests/graph tests/secrets tests/service/test_cli_and_factory.py`.
- [ ] After plan 6: run frontend tests/build, full Python suite, formatting, wheel build, and clean-wheel smoke test.
- [ ] Confirm `.claude/launch.json` and `.planning/gov-sec-parity-progress.md` remain outside implementation commits unless the user explicitly requests otherwise.

## Final verification

```bash
uv run pytest -q
uv run ruff check src/
uv run ruff format --check src/
npm --prefix frontend test -- --run
npm --prefix frontend run build
uv build --wheel
git diff --check
```

Expected: all commands exit 0; pytest has no unawaited-coroutine warning; only documented sandbox/FastAPI warnings may remain.

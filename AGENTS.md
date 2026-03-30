# Zeroth — Agent Guidelines

## Project

Zeroth is a governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services. 

## Context Efficiency

When starting a task, read ONLY what you need:
1. Your task's section in `PROGRESS.md` (root) — current state
2. The relevant `phases/phase-N-*/PLAN.md` — detailed requirements
3. Do NOT read root `PLAN.md` (master spec, not implementation guide)
4. Do NOT read other phases' plans unless your task depends on them

## Implementation Tracking

- `PROGRESS.md` (root) — single source of truth for all task progress and iteration log
- `phases/phase-N-*/PLAN.md` — detailed requirements per phase
- `phases/phase-N-*/artifacts/` — test output, evidence

## Build & Test

```bash
uv sync                    # install/update deps
uv run pytest -v           # run tests
uv run ruff check src/     # lint
uv run ruff format src/    # format
```

## Project Layout

```
src/zeroth/     # main package
tests/          # pytest tests
```

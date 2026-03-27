# Live Research Audit Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deployment-scoped FastAPI live scenario that audits the Zeroth codebase using real agent, tool, executable-unit, memory, approval, policy, and thread flows.

**Architecture:** The scenario lives under `live_scenarios/research_audit/` and bootstraps a published graph plus deployment into SQLite, then wires custom agent runners, executable units, memory connectors, thread state, and policy guard into the existing service wrapper. Public interaction stays on the existing HTTP API while helper scripts handle seeding, serving, and sample queries.

**Tech Stack:** Python 3.12, FastAPI, Zeroth service bootstrap/runtime, SQLite, GovernAI LLM wrapper, httpx, wrapped command executable units

---

### Task 1: Add failing scenario tests

**Files:**
- Create: `tests/live_scenarios/test_research_audit.py`
- Test: `tests/live_scenarios/test_research_audit.py`

- [ ] **Step 1: Write the failing bootstrap and API tests**

Cover:

- scenario app bootstraps a deployment and exposes `/health`
- scenario run succeeds with deterministic provider and tool outputs
- scenario approval branch pauses and resumes through the approval API
- scenario thread reuse preserves memory/thread state across runs
- strict policy mode terminates the run before execution

- [ ] **Step 2: Run the scenario test file to verify it fails**

Run: `uv run pytest tests/live_scenarios/test_research_audit.py -v`

Expected: FAIL because the scenario package and bootstrap do not exist yet.

### Task 2: Implement scenario contracts, tools, and graph bootstrap

**Files:**
- Create: `live_scenarios/research_audit/__init__.py`
- Create: `live_scenarios/research_audit/contracts.py`
- Create: `live_scenarios/research_audit/tools.py`
- Create: `live_scenarios/research_audit/bootstrap.py`
- Create: `live_scenarios/research_audit/normalize_evidence.py`
- Modify: `tests/live_scenarios/test_research_audit.py`

- [ ] **Step 1: Implement scenario contracts and graph builders**

Include structured input/output models for planner, reviewer, approval, and final output.

- [ ] **Step 2: Implement repo-search, file-read, web-search, and fetch-url executable units**

Support deterministic test doubles and env-driven live behavior.

- [ ] **Step 3: Implement scenario bootstrap**

Wire:

- contract registration
- graph creation/publish/deploy
- executable unit registry/runner
- deterministic or live provider adapters
- thread resolver and repository-backed thread state
- memory connector resolver
- policy guard
- FastAPI app creation

- [ ] **Step 4: Run the scenario tests again**

Run: `uv run pytest tests/live_scenarios/test_research_audit.py -v`

Expected: either partial failures on behavior gaps or full pass if the first implementation is sufficient.

### Task 3: Add runnable scripts and usage docs

**Files:**
- Create: `live_scenarios/research_audit/run_server.py`
- Create: `live_scenarios/research_audit/run_queries.py`
- Create: `live_scenarios/README.md`

- [ ] **Step 1: Implement the server entrypoint**

It should create the scenario app from env and launch Uvicorn.

- [ ] **Step 2: Implement a query runner**

It should:

- submit a run
- poll until completion or approval pause
- optionally auto-resolve approval when requested
- print the final result and source summary

- [ ] **Step 3: Document runtime requirements and commands**

Document:

- required env vars
- local deterministic mode
- live mode
- example audit queries

### Task 4: Verify locally and run live queries when possible

**Files:**
- Create: `phases/phase-5-integration/artifacts/test-live-scenario-2026-03-27.txt`
- Create: `phases/phase-5-integration/artifacts/smoke-live-scenario-2026-03-27.txt`
- Create: `phases/phase-5-integration/artifacts/live-query-research-audit-2026-03-27.txt`

- [ ] **Step 1: Run targeted tests and capture output**

Run: `uv run pytest tests/live_scenarios/test_research_audit.py -v | tee phases/phase-5-integration/artifacts/test-live-scenario-2026-03-27.txt`

- [ ] **Step 2: Run a deterministic local smoke scenario and capture output**

Run the query runner against the local FastAPI app without external credentials.

- [ ] **Step 3: Run live queries if credentials are available**

Queries:

- `Find likely bugs in Zeroth service bootstrap around thread handling and policy wiring.`
- `Find likely bugs in tool attachment execution, approval resume, and audit persistence.`

- [ ] **Step 4: Summarize findings and residual risks**

Include any runtime issues exposed by the live scenario itself.

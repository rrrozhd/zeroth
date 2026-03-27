# Live Research Audit Scenario Design

**Date:** 2026-03-27
**Status:** draft for implementation

## Goal

Add a deployment-scoped FastAPI live scenario under `live_scenarios/` that exercises Zeroth across the widest realistic surface area: external model calls, tool calls, wrapped-command executable units, shared memory, conditional branching, human approval pause/resume, thread continuity, policy enforcement, and deployment-bound HTTP execution.

The scenario should be directly usable for live implementation audits of Zeroth itself.

## Scenario

The scenario app is a "research and decision" service for code-audit questions.

The user submits a question such as:

- "Find likely bugs in the service bootstrap and thread handling."
- "Scan the live implementation for policy and tool-execution risks."

The deployed graph then:

1. Runs a planner agent to convert the question into an investigation plan.
2. Runs a researcher agent that can call repo-inspection and optional external-web tools.
3. Runs an executable unit that normalizes and deduplicates gathered evidence.
4. Runs a reviewer agent that scores confidence and decides whether approval is required.
5. Branches either to a human approval node or directly to final synthesis.
6. Runs a final agent that returns the answer in a structured response.

## Functional Coverage

The scenario is intentionally designed to hit the current Zeroth runtime seams:

- `FastAPI` deployment wrapper
- deployment snapshot and contract pinning
- agent runtime with real external model provider
- tool calling through declared tool attachments
- executable unit execution through wrapped commands
- shared memory connector across agents
- condition-based branching
- human approval pause and resume through the approval API
- thread continuity across multiple runs
- policy guard denial paths
- persisted runs, checkpoints, threads, approvals, and audits in SQLite

## Architecture

### Public surface

The scenario will reuse the existing deployment wrapper API:

- `GET /health`
- `POST /runs`
- `GET /runs/{run_id}`
- `GET /deployments/{deployment_ref}/metadata`
- `GET /deployments/{deployment_ref}/approvals`
- `POST /deployments/{deployment_ref}/approvals/{approval_id}/resolve`

No new public service routes are required for the first version.

### Scenario bootstrap

`live_scenarios/research_audit/bootstrap.py` will:

- create/open the SQLite database
- register scenario contracts
- create and publish the graph
- create the deployment snapshot
- wire agent runners, executable units, memory connectors, and policy guard
- return a deployment-scoped FastAPI app

The bootstrap will explicitly wire runtime pieces that current generic service bootstrap does not provide by default:

- `RepositoryThreadResolver`
- `RepositoryThreadStateStore`
- `PolicyGuard`
- shared-memory connector registry

### Graph shape

Nodes:

1. `plan`
2. `research`
3. `normalize_evidence`
4. `review`
5. `approval`
6. `finalize`

Branching:

- `plan -> research` when external or repo evidence is needed
- `plan -> finalize` when the planner can answer directly
- `review -> approval` when `payload.requires_approval`
- `review -> finalize` when approval is not required

The first version stays acyclic to avoid adding a review-loop branch before the live tool surface is stable. Loop coverage already exists in repo tests; the live scenario should prioritize tool, policy, approval, and memory behavior.

## Tools and executable units

The researcher agent gets declared tool attachments backed by executable units:

- `repo_search`: wrapped command using `rg`
- `read_file_excerpt`: native Python executable unit for bounded local file reads
- `web_search`: native Python executable unit using an env-driven HTTP search provider
- `fetch_url`: native Python executable unit using `httpx`

`normalize_evidence` will be a wrapped-command executable unit that reads JSON stdin and emits normalized JSON stdout. This keeps a real subprocess path in the live scenario.

## Memory model

The planner and researcher agents share one thread-scoped key-value memory binding:

- planner writes investigation goals
- researcher appends gathered evidence
- reviewer reads the accumulated evidence summary

This should leave visible thread memory bindings and memory-access audit records.

## Policy model

The graph will bind a baseline policy that allows read-only investigation capabilities:

- `network_read`
- `filesystem_read`
- `memory_read`
- `memory_write`

The external search tool can also require `secret_access` when an API key-backed provider is used.

The bootstrap will expose a strict mode that deliberately denies one required capability so the same app can be used to validate policy-rejection behavior.

## Provider model

The live provider will be environment-driven and should default to OpenAI-compatible credentials through `GovernedLLM.from_chat_openai(...)`.

Required env:

- `OPENAI_API_KEY`

Optional env:

- `OPENAI_MODEL` default `gpt-4o-mini`
- `OPENAI_BASE_URL`
- `LIVE_SCENARIO_SEARCH_PROVIDER`
- `LIVE_SCENARIO_SEARCH_API_KEY`

If model credentials are missing, the code should still allow local tests to run with deterministic adapters.

## Expected outputs

The final run output should be structured, not free-form only:

- `answer`
- `summary`
- `findings`
- `confidence`
- `sources`
- `approval_used`

## Bug-scan focus

The first live queries should target implementation seams already visible in the current codebase:

1. Deployment bootstrap omissions around thread resolver and policy guard.
2. Tool attachment and permission mismatch behavior under real model-issued tool calls.
3. Audit and approval API behavior under multi-step thread reuse.

## Verification

Implementation verification will include:

- targeted scenario tests
- a local FastAPI smoke run with deterministic adapters
- a live-query run if external model credentials are present

## Self-review

This spec is intentionally scoped to the existing service wrapper and does not require adding a deployment-management API or UI.

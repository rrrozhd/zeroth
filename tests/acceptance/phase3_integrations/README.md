# Phase 3 — customer integrations: G3 reviewer package

**Reviewer decision: pending independent review.** Nothing here is accepted:
A07, A08, G3-core and G3-common remain unaccepted in `manifest.json`.
This package lets an independent reviewer reproduce every row's evidence
from a clean environment.

## Candidate

`manifest.json["candidate"]` records the original integration branch
(`acceptance/phase-3-integrations`), its base `faa16232`, head commit and
commits since the base. These identify the recorded candidate, not the current
checkout. The SDK contract (`zeroth.protocol` events to
`/v1/executions` and `/v1/outcomes`), the server's ingestion, schemas and
pricing are unchanged; everything lives in the SDK source tree, the recipe
projects and this harness.

## What the reviewer reproduces

1. `uv sync --all-extras --group integration-conformance` in the worktree
   (Python 3.12; Node ≥ 23.6 for the TypeScript rows).
2. `uv run pytest tests/acceptance/phase3_integrations -q` runs the contract,
   every row, the lifecycle checks and this package's integrity test. Rows C04
   and C05 build pinned virtual environments (network on first run); C06 runs
   `npm ci` from its committed lock.
3. Each recipe README under `packaging/sdk/recipes/<row>/` is the customer's
   installation and change guide; each has a `check.*` driver that reproduces
   the frozen workload against any Zeroth deployment without this test suite.

The tests rewrite evidence files with the current environment's package list,
timings and client-observed duplicates. Review those changes before retaining a
new run. Installed-package records use `${REPO_ROOT}` for the repository location
so they do not retain a developer's absolute checkout path.

## The shared contract

`INTERFACE.md` is the one mapping every row follows: one physical call, one
`cost_role="charge"` with a stable `event_id`/`charge_id`; framework
aggregates as money-free summaries; estimated cost from the pinned rate card
for four models, unmeasured (never zero) for missing usage, unlisted models or
undeclared providers; outcomes and their maturity are the customer's.
`workload.py` and `expected_ledger.json` are frozen (digests in
`manifest.json["frozen"]`); `ledger.py` reads only stored rows.

## Rows

| Row | Setup | Recipe | Checks | Pins verified |
| --- | --- | --- | --- | --- |
| C01 | direct OpenAI / Anthropic Python | `c01_direct` | `test_c01_direct.py` (11) | openai 1.66.0 / anthropic 0.40.0; openai 3.11.0 / anthropic 1.4.0 |
| C02 | LangGraph / LangChain | `c02_langgraph` | `test_c02_langgraph.py` (8) | langchain 1.0.0 / langgraph 1.0.0; 1.4.0 / 1.2.11 |
| C03 | OpenAI Agents SDK | `c03_openai_agents` | `test_c03_openai_agents.py` (7) | openai-agents 0.3.0; 0.22.2 |
| C04 | CrewAI crews and flows | `c04_crewai` | `test_c04_crewai.py` (2 pinned envs) | crewai 1.14.0; 1.15.21 |
| C05 | AutoGen AgentChat 0.7.x | `c05_autogen` | `test_c05_autogen.py` (3) | autogen-agentchat 0.7.0; 0.7.5 |
| C06 | Vercel AI SDK (TypeScript) | `c06_vercel` | `test_c06_vercel.py` (4) | ai 7.0.97 on Node 26.5 |
| C07 | explicit Python / TypeScript workers | `c07_workers` | `test_c07_workers.py` (5) | SDK source; Node 26.5 (+ strip-types flag path) |

Every row reproduces the frozen nine-family workload for two versions with an
exact ledger, except C05, where AgentChat exposes no cache split and the
harness asserts the exact repricing magnitude on the cached run instead.
`manifest.json["rows"]` holds, per row: recipe location, framework/provider/
runtime versions, lock or package digests, identity mapping, cost categories
and provenance, supported and excluded modes, evidence links.

## Lifecycle

`test_lifecycle.py`: two tenants running C01 and C02 concurrently with
per-tenant ledgers; a worker crash whose stored rows equal its own log and a
rerun delivering the rest once; 402/409/422/503, a dropped connection and an
outage window retained in `Recorder.lost` by SDK error class and replayed to
an exact ledger; equal provider and tool call counts with and without
instrumentation; adapter overhead recorded as a number with its method
(`manifest.json["lifecycle"]`), not judged: the A11 SLO is unset.

## Required failures

`manifest.json["required_failures"]` classifies each of the nine required
failure classes per row as tested, partially, not exercised or NOT_RUN. Two
are NOT_RUN everywhere: a denied key (401) is never injected, and a missing
outcome is by contract missing rather than a failure.

## Not run, and what each needs

`manifest.json["not_run"]`: live-provider evidence for every row, Azure OpenAI,
Google Gemini, a live OpenAI-compatible proxy, an independent recipe reviewer,
a Node 22 binary, the A11 overhead threshold, and SDK publication. Each entry
names the owner input it needs (credentials and spend allowance, a deployment
mapping, a reviewer, a runtime, a D06 decision, a release).

## Limitations the reviewer should weigh

- All provider traffic is replayed fixtures; request ids, invoices and
  rate-card drift are unverified.
- The rate card exists three times (SDK, C07 worker, C06 capture); drift is
  caught only by reconciliation at the pinned litellm.
- C03 records agent runs as aggregates inside the run summary rather than as
  separate summary events (the frozen ledger pins one summary per run).
- C04 patches `Crew.kickoff` at class level to bind crews to runs for
  async-execution tasks; an application bypassing kickoff cannot attribute them.
- C05 cannot price cached tokens exactly through AgentChat's usage surface.
- Evidence and records are agent-reported; no independent adjudication yet.

`manifest.json["follow_ups"]` lists the concrete follow-ups.

## Full suite and environment interactions

`manifest.json["full_suite"]` preserves the original candidate's reported pytest
result (14,177 passed; 96 failures and 6 errors) and failure attribution. Its
original log is not included in this repository; it is not a current test result.

CI installs all dependency groups, including `integration-conformance`.
That group brings `httpx2` through anthropic ≥ 1.0, and Starlette's `TestClient`
then returns `httpx2.Response` objects. The SDK test forwarding handlers convert
these responses to `httpx.Response` before returning them to `httpx.MockTransport`.

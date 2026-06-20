# Requirements: Zeroth

**Defined:** 2026-04-09 (v2.0) · **Updated:** 2026-04-13 (v4.1 milestone started)
**Core Value:** Teams can author and operate governed multi-agent workflows without sacrificing production controls, auditability, or deployment rigor.

## v4.1 Requirements — Platform Hardening & Missing Implementations

v4.1 closes gaps between declared capabilities and actual implementations across orchestration, storage, templates, and resilience.

### Orchestration Composition (ORCH)

- [ ] **ORCH-01**: Fan-out node can invoke subgraphs concurrently (parallel subgraph invocations)
- [ ] **ORCH-02**: Subgraphs can contain fan-out nodes (nested fan-out composition)
- [ ] **ORCH-03**: All declared reduce merge strategies are implemented (reduce, merge, custom — not just collect)
- [ ] **ORCH-04**: Merge strategy selection is validated at graph registration time

### Artifact Storage Backends (ARTS)

- [ ] **ARTS-01**: Artifacts can be stored to and retrieved from AWS S3
- [ ] **ARTS-02**: Artifacts can be stored to and retrieved from Google Cloud Storage
- [ ] **ARTS-03**: Storage backend is selectable via configuration (Redis, filesystem, S3, GCS)
- [ ] **ARTS-04**: Existing Redis and filesystem backends continue working unchanged

### Template Registry Persistence (TREG)

- [ ] **TREG-01**: Templates persist across restarts via SQLAlchemy storage backend
- [ ] **TREG-02**: Template CRUD operations work against database backend
- [ ] **TREG-03**: In-memory registry remains available as a fast-path option

### Circuit Breaker Durability (CBRK)

- [ ] **CBRK-01**: HTTP circuit breaker state persists in Redis across restarts
- [ ] **CBRK-02**: Circuit breaker state is shared across horizontal workers
- [ ] **CBRK-03**: Circuit breaker degrades gracefully to in-memory if Redis unavailable

### Template Memory Binding (TMB)

- [x] **TMB-01**: Agent nodes can declare memory connector bindings that are resolved at dispatch time
- [x] **TMB-02**: `get` mode reads a single key from a connector into a named template variable
- [x] **TMB-03**: `scan` mode reads all entries with a given prefix into a dict-typed template variable
- [x] **TMB-04**: Binding keys support `{input.field}`, `{state.field}`, `{run.run_id}` placeholder substitution
- [x] **TMB-05**: `max_items` truncates scan results; `default` is returned when a key is absent
- [x] **TMB-06**: Unknown connector references raise `MemoryBindingResolutionError` at dispatch time

### v4.1 Future Requirements

Deferred to future release. Tracked but not in current roadmap.

- **ARTS-05**: Azure Blob Storage backend for artifacts
- **ARTS-06**: Artifact lifecycle policies (auto-expiry, archival tiers)
- **TREG-04**: Template versioning with rollback support
- **TREG-05**: Template import/export for cross-environment migration

### v4.1 Out of Scope

| Feature | Reason |
|---------|--------|
| Azure Blob artifact storage | S3 + GCS cover primary clouds; Azure can use S3 compatibility |
| Template marketplace / sharing | Registry is internal; sharing is a separate product concern |
| Circuit breaker UI dashboard | Operational tooling — not core platform hardening |
| Distributed circuit breaker consensus | Redis shared state is sufficient; no need for Raft/Paxos |

---

## Identified Gaps — Candidate v4.2 (Agent Quality, Safety & Observability)

Surfaced by a 2026-06-20 codebase gap analysis (see `.planning/GAP-ANALYSIS.md`). These are
confirmed-absent or weak-partial capabilities that cut against the "governed, production-grade
multi-agent" positioning. Not yet roadmapped to phases; logged here for traceability.

### Agent Evaluation Harness (EVAL) — shipped 2026-06-20

New `zeroth.core.eval` module: datasets, scorers, runner, and a CI gate. Decoupled from
any executor via a `target` callable (`async (input) -> output`; wrap an `AgentRunner` or a
graph run). Errors are first-class — a judge/target failure is an *errored* case, never a
silent score of zero, and errored cases count against the pass rate.

- [x] **EVAL-01**: `EvalDataset`/`EvalCase` reference format (input + expected + optional per-case rubric), loadable from JSONL/records
- [x] **EVAL-02**: `LLMJudgeScorer` (configurable rubric, per-case override) using a provider adapter with structured output; provider/parse failures are errored, not zero
- [x] **EVAL-03**: Deterministic scorers — exact-match, contains, regex, Pydantic-schema (the project's schema mechanism, not raw JSON-Schema), and arbitrary predicate
- [x] **EVAL-04**: `EvalReport` (pass-rate, error-rate, per-scorer means) + `gate()` raising below an **absolute** pass-rate floor — baseline-diff regression deferred; errored cases can't inflate the rate
- [x] **EVAL-05**: Quality scoring is a distinct module from `agent_runtime.OutputValidator` (shape); `SchemaScorer` is the one shape-as-quality check among the quality scorers

### Model-Boundary Safety (MBND) — shipped 2026-06-20

Implemented in `zeroth.core.agent_runtime.sanitization` (`ToolOutputSanitizer`,
`HeuristicInjectionScreener`), wired into `AgentRunner._resolve_tool_calls` and the
memory path in `PromptAssembler`. Controlled by `AgentConfig.tool_output_safety`
(enabled by default); per-tool cap via `ToolAttachmentManifest.max_output_chars`.

- [x] **MBND-01**: Tool/function results are sanitized (escaped, length-capped) before re-injection into the model
- [x] **MBND-02**: Memory-sourced content carries provenance/untrusted markers when injected into prompts
- [x] **MBND-03**: Configurable prompt-injection screening on untrusted tool/memory input
- [x] **MBND-04**: Per-tool output size limits enforced independently of timeout

### Content Safety Guardrails (SAFE) — shipped 2026-06-20

Implemented in `zeroth.core.guardrails.content` (`ContentGuardrail`, `PIIFilter`,
`BlocklistFilter`), applied at the agent input/output boundary in `AgentRunner` via
`AgentConfig.content_safety` (**opt-in**, disabled by default — it imposes a policy on
the app's own typed data). Block mode raises `AgentContentBlockedError`, which carries an
`audit_record` so the orchestrator persists a `rejected` NodeAuditRecord with the findings.
Scope: guards the agent's typed `input` and `output`; untrusted tool/memory output is
covered separately by MBND.

- [x] **SAFE-01**: Pluggable content filter on agent input/output (PII + blocklist; custom `ContentFilter` injectable) — implemented as an agent-boundary guardrail, not a standalone graph node
- [x] **SAFE-02**: PII detection/redaction option on agent input and output (email, SSN, credit-card w/ Luhn, phone)
- [x] **SAFE-03**: Safety guardrails emit audit events (`content_safety` in the node audit; `rejected` record on block) and can flag / redact / hard-block

### Distributed Tracing (OBS) — shipped 2026-06-20

Implemented in `zeroth.core.observability.tracing` (`start_span`, `configure_tracing`).
OpenTelemetry is an optional `[otel]` extra; tracing is **off by default** and a
zero-overhead no-op until enabled via `ZerothSettings.tracing` (wired in
`bootstrap_service`). Spans wrap the orchestrator run, node dispatch, fan-out, subgraph,
agent run, and tool call; fan-out branches inherit the parent span via asyncio
contextvar propagation.

- [x] **OBS-01**: Spans across orchestrator run/drive, node dispatch, agent run, tool call, and subgraph hops
- [x] **OBS-02**: Correlation ID attached to spans; trace context propagates across fan-out branches (asyncio tasks)
- [x] **OBS-03**: Configurable OTLP/HTTP exporter; spans carry the run/node/correlation identifiers that key the Prometheus metrics and audit records

### Retrieval / RAG Node (RAG) — shipped 2026-06-20

`RetrievalNode` (a first-class graph node type in `zeroth.core.graph`, dispatched by the
orchestrator) queries a memory connector via the uniform `search({"text", "limit"})`
interface (vector connectors do semantic top-k) and outputs the retrieved chunks under a
configurable key for a downstream agent to ground on. Ingestion lives in
`zeroth.core.rag.ingestion`.

- [x] **RAG-01**: First-class `RetrievalNode` type that queries a vector connector and outputs grounded context for a downstream node to consume
- [x] **RAG-02**: Document chunking + write pipeline (`chunk_text`, `ingest_documents`) feeding the retrieval store — **embedding delegated to the connector**; chunk attribution travels via the `{source_id}#{index}` key (the connector `write` interface carries no metadata field)
- [x] **RAG-03**: Retrieved chunks audited with source attribution (query + per-chunk id/metadata; chunk bodies are not written to the audit log)

### Provider Resilience (PRES) — shipped 2026-06-20

Composable `ProviderAdapter` wrappers in `zeroth.core.agent_runtime.resilience`
(`FallbackProviderAdapter`, `CachingProviderAdapter`, `InMemoryResponseCache`), stacked
like `econ.InstrumentedProviderAdapter`. Opt-in: nothing is wrapped unless the caller
composes them (e.g. `CachingProviderAdapter(FallbackProviderAdapter.across_models(...))`).

- [x] **PRES-01**: Model fallback chains — `FallbackProviderAdapter` tries an ordered list of targets (per-target model override), failing over on transient provider errors (reuses the retry classifier); raises the last error if all fail
- [x] **PRES-02**: Response caching keyed on prompt + params — **exact-match** via `CachingProviderAdapter` (tool-call responses skipped by default). **Semantic** caching is deferred; the `ResponseCache` protocol is the extension point for it.

### Re-evaluate Current Out-of-Scope Calls

The 2026-06-20 analysis flags two prior "Out of Scope" deferrals as **contestable** and worth
revisiting before 1.0. Defensible deferrals (mobile apps, custom LLM hosting) are unchanged.

| Deferral | Why contestable |
|----------|-----------------|
| Real-time streaming ("async sufficient") | No time-to-first-token for interactive/agentic UX; SSE is table stakes for agent APIs |
| Model fallback chains | ✅ Resolved 2026-06-20 — `FallbackProviderAdapter` (PRES-01) provides automatic failover |

---

## v3.0 Requirements — Core Library Extraction, Studio Split & Documentation

v3.0 is a packaging and documentation milestone. No new runtime features. Deliverables: ship `zeroth-core` on PyPI under the `zeroth.core.*` namespace, move the Vue Studio to its own public repo, write in-depth documentation for every subsystem, and formalize the monolith archive.

### Packaging (PKG)

- [ ] **PKG-01**: `econ-instrumentation-sdk` (Regulus SDK) is published to PyPI with a stable version, replacing the current local file-path dependency
- [ ] **PKG-02**: `zeroth-core` is published to PyPI as a pip-installable library and `pip install zeroth-core` succeeds in a clean virtualenv
- [ ] **PKG-03**: `pyproject.toml` declares optional-dependency extras for every swappable backend (`[memory-pg]`, `[memory-chroma]`, `[memory-es]`, `[dispatch]`, `[sandbox]`, `[all]`), with each extra verified installable and documented
- [ ] **PKG-04**: Repository root contains `CHANGELOG.md` (keepachangelog format), `LICENSE`, and `CONTRIBUTING.md` suitable for a public PyPI release
- [ ] **PKG-05**: PyPI releases are published via GitHub Actions using trusted publisher (OIDC), not a long-lived API token
- [ ] **PKG-06**: Installing `zeroth-core` from a clean environment and running the Getting Started hello example works end-to-end (acceptance test for the whole packaging stack)

### Namespace Rename (RENAME)

- [ ] **RENAME-01**: All Python source is relocated from `zeroth.*` to `zeroth.core.*` with zero deletions and zero functional changes (pure rename)
- [ ] **RENAME-02**: The package is a PEP 420 namespace package — no top-level `zeroth/__init__.py` — so future sibling packages (`zeroth.studio`, `zeroth.ext.*`) can coexist under `zeroth.*`
- [ ] **RENAME-03**: All internal imports, string references, entry points, and console scripts point at the new `zeroth.core.*` paths
- [ ] **RENAME-04**: The existing test suite (280+ tests) passes against the renamed package with no skips or regressions
- [ ] **RENAME-05**: Docstring coverage on the public surface of `zeroth.core.*` reaches ≥90% (measured by `interrogate`) with a consistent style (Google-style)

### Documentation Content (DOCS)

- [x] **DOCS-01**: Landing page presents a 10-line hello-world, install snippet, and a "Choose your path" split between embedding as a library and running as a governed service
- [x] **DOCS-02**: Getting Started is a single linear 3-section tutorial (install → first graph with one agent/tool/LLM → run in service mode with an approval gate), reaching first working output in under 5 minutes and completing in under 30
- [x] **DOCS-03**: Every major `zeroth.core.*` subsystem has a Concept page (what it is, why it exists, mental model, where it fits) — covering graph, orchestrator, agents, execution units, memory, contracts, runs, conditions, mappings, policy, approvals, audit, secrets, identity, guardrails, dispatch, economics, storage, service, threads
- [x] **DOCS-04**: Every major subsystem has a Usage Guide (Overview → Minimal example → Common patterns → Pitfalls → Reference cross-link) paired with its Concept page
- [x] **DOCS-05**: A Governance Walkthrough tutorial shows an end-to-end run where an approval gate stops execution, an auditor reviews the trail, and a policy blocks a tool call (Zeroth's differentiator vs. LangGraph/CrewAI)
- [x] **DOCS-06**: A Cookbook section contains at least 10 cross-subsystem recipes at launch (examples: add a human approval step, attach pgvector memory to a node, cap a run's budget with Regulus, sandbox a Python execution unit, retry a failed webhook from the DLQ)
- [x] **DOCS-07**: Python API Reference is auto-generated from docstrings via mkdocstrings + Griffe for the full public surface
- [x] **DOCS-08**: HTTP API Reference is rendered from the FastAPI OpenAPI spec and published alongside the Python reference
- [x] **DOCS-09**: Configuration Reference is auto-generated from pydantic-settings schemas and documents every env var, default, and secret
- [x] **DOCS-10**: Deployment Guide covers local dev, docker-compose, standalone service mode, embedded-in-host-app mode, and deployments with/without the Regulus companion service
- [x] **DOCS-11**: Migration Guide explains how to move from the monolithic `zeroth.*` layout to `zeroth.core.*` (import rename pattern, econ SDK path swap, env var changes, Docker image retag)
- [x] **DOCS-12**: An `examples/` directory at the repo root contains runnable `.py` files (not notebooks) exercising the main subsystems, and CI smoke-tests each on every main commit

### Documentation Site Infrastructure (SITE)

- [x] **SITE-01**: Documentation is built with mkdocs-material using explicit Diátaxis IA (Tutorials / How-to Guides / Concepts / Reference as four top-level sections)
- [x] **SITE-02**: A GitHub Actions workflow builds and deploys the docs site to a public URL on every commit to `main`
- [ ] **SITE-03**: Pull request previews deploy a rendered version of the changed docs so reviewers see the output before merge
- [x] **SITE-04**: The docs site includes built-in search and an automatically generated site map

### Studio Repo Split (STUDIO)

- [x] **STUDIO-01**: A new public repository `rrrozhd/zeroth-studio` contains the Vue 3 + Vue Flow frontend with its full git history preserved (subtree or filter-repo)
- [x] **STUDIO-02**: `zeroth-studio` has its own independent CI pipeline (lint, typecheck, build, test) passing on its default branch
- [x] **STUDIO-03**: `zeroth-studio` consumes `zeroth-core` only via HTTP/OpenAPI — no Python imports, no shared source tree
- [x] **STUDIO-04**: Both repositories' READMEs cross-link to each other, and a cross-repo compatibility matrix (`zeroth-studio` version ↔ `zeroth-core` version) is documented and maintained in at least one of them
- [x] **STUDIO-05**: The frontend types used by `zeroth-studio` are generated from the `zeroth-core` OpenAPI spec via `openapi-typescript`, keeping the two in sync without manual edits

### Monolith Archive (ARCHIVE)

- [ ] **ARCHIVE-01**: A multi-layer archive of the pre-split monolithic repo exists and is documented: local tarball, local bare mirror, and GitHub repository `rrrozhd/zeroth-archive`
- [ ] **ARCHIVE-02**: All 36 worktree branches, both stashes, and the detached-HEAD worktree from the ad-hoc split work are preserved in the archive and recoverable
- [ ] **ARCHIVE-03**: The archive repository carries a visible "this repo is archived — see rrrozhd/zeroth-core and rrrozhd/zeroth-studio" notice in its README and repo description

## v4.0 Requirements — Platform Extensions for Production Agentic Workflows

v4.0 adds five runtime subsystems (resilient HTTP, prompt templates, context window management, parallel fan-out/fan-in, subgraph composition) and a capstone integration phase.

### Resilient HTTP Client (HTTP)

- [x] **HTTP-01**: Platform-provided async HTTP client available to agent tools and executable units, configurable per-node or per-tool
- [x] **HTTP-02**: Configurable retry with exponential backoff and jitter; retryable status codes configurable
- [x] **HTTP-03**: Per-endpoint circuit breaker with configurable failure threshold and reset timeout
- [x] **HTTP-04**: Shared or per-tenant connection pools with configurable limits
- [x] **HTTP-05**: External HTTP calls gated by capabilities, logged in audit records, subject to rate limiting
- [x] **HTTP-06**: HTTP client resolves auth headers/tokens from SecretResolver automatically

### Prompt Template Management (TMPL)

- [x] **TMPL-01**: Template registry stores and versions prompt templates by name with create/retrieve/list API
- [x] **TMPL-02**: Templates support variable interpolation using Jinja2 SandboxedEnvironment preventing injection
- [x] **TMPL-03**: Agent node references template by name+version; resolved and rendered at runtime before LLM invocation
- [x] **TMPL-04**: Rendered prompt in audit records; secret variables automatically redacted

### Context Window Management (CTXW)

- [x] **CTXW-01**: Token count tracked via litellm.token_counter, updated after each LLM invocation
- [x] **CTXW-02**: Configurable threshold triggers compaction, default observation masking
- [x] **CTXW-03**: Three pluggable strategies: truncation, observation masking, LLM summarization
- [x] **CTXW-04**: Compaction results persist in thread memory; optional archive of originals
- [x] **CTXW-05**: Per-agent-node configurable settings on AgentNodeData

### Parallel Fan-Out/Fan-In (PARA)

- [x] **PARA-01**: Node spawns N parallel branches from output, synchronization barrier collects with deterministic ordering by branch index
- [x] **PARA-02**: Per-branch isolated execution context; best-effort and fail-fast modes
- [x] **PARA-03**: Policy, audit, and contract validation apply independently per branch
- [x] **PARA-04**: Cost attribution per branch, BudgetEnforcer pre-reservation, ExecutionSettings guardrails as sum across branches
- [x] **PARA-05**: Complete branch isolation: separate visit counts, audit trail, failure tracking
- [x] **PARA-06**: Fan-out integrates without breaking sequential execution

### Subgraph Composition (SUBG)

- [x] **SUBG-01**: SubgraphNode with graph_ref, version, thread_participation, max_depth
- [x] **SUBG-02**: Orchestrator resolves subgraph at runtime, executes via recursive _drive()
- [x] **SUBG-03**: Child Run linked to parent via parent_run_id
- [x] **SUBG-04**: Parent governance acts as ceiling — subgraph can restrict not relax
- [x] **SUBG-05**: Thread participation configurable: inherit shares thread_id, isolated creates new
- [x] **SUBG-06**: Approval pauses propagate to parent, resolution cascades back
- [x] **SUBG-07**: Depth tracking with SubgraphDepthLimitError
- [x] **SUBG-08**: Node IDs namespaced with subgraph:{ref}:{depth}: prefix

### Integration & Service Wiring (D)

- [ ] **D-01**: All v4.0 subsystems on ServiceBootstrap after bootstrap_service()
- [ ] **D-02**: Cross-feature interactions tested (parallel+artifacts, parallel+context, parallel+templates, subgraph+templates; SubgraphNode-in-parallel rejected)
- [ ] **D-03**: Artifact retrieval REST endpoint (GET /v1/artifacts/{id})
- [ ] **D-04**: Template CRUD REST endpoints (GET/POST/DELETE /v1/templates)
- [ ] **D-05**: SubgraphNode-in-parallel rejected with clear validation error
- [ ] **D-06**: Full test suite passes with zero new failures (backward compatibility)
- [ ] **D-07**: In-repo documentation references new v4.0 API capabilities

## Deferred to v0.2.x of `zeroth-core`

Tracked but out of scope for v3.0 milestone completion. Will be opened as new phases once a first external user has validated the core library.

- [ ] **FUTURE-01**: LibCST codemod (`python -m zeroth.core.codemods.rename_from_monolith`) that automatically rewrites `zeroth.*` imports to `zeroth.core.*` in consumer codebases
- [ ] **FUTURE-02**: HTTP/curl tabs added inline to every subsystem usage guide (initially Python-only)
- [ ] **FUTURE-03**: Extension-point guides — "Writing a custom memory connector", "Writing a custom LLM provider", "Writing a custom execution unit", "Writing a custom judge"
- [ ] **FUTURE-04**: Docstring coverage badge in README
- [ ] **FUTURE-05**: Governance case studies (real-world workflows with captured audit trails)
- [ ] **FUTURE-06**: Algolia DocSearch (if docs traffic warrants it)

## Deferred to `zeroth-studio` repo (v2.0 phases 24-26)

These requirements are **not cancelled** — they continue in the new `zeroth-studio` repo after the split and will be roadmapped there separately.

- [ ] **API-02** (from v2.0): Studio receives real-time updates via WebSocket (execution status, validation, presence)
- [ ] **API-04** (from v2.0): Studio can trigger workflow execution and receive per-node status updates
- [ ] **AUTH-01** through **AUTH-05** (from v2.0): Canvas execution & AI authoring (model selector, prompt editor, tool attachment, data-flow tooltips, per-node results)
- [ ] **GOV-01** through **GOV-07** (from v2.0): Governance visualization (approval gates, audit trails, sandbox badges, RBAC-aware canvas, cost/budget display, env switching)
- [ ] **COLLAB-01**, **COLLAB-02** (from v2.0): Graph version diff view, collaborative presence indicators

## Out of Scope

Explicitly excluded for v3.0. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Multi-version docs site with version dropdown (`mike` plugin) | Pre-1.0 — API still shifts, stale versions confuse more than help. Revisit at 1.0. |
| Translations / multi-language docs | FastAPI has 12 language maintainers; we have one team. Translations rot faster than code. |
| Hand-written API reference for every class | 22K LOC — impractical and goes stale on day one. Use mkdocstrings instead. |
| Jupyter notebook examples | Hard to version-control, diff, and CI-test. Use plain `.py` files. |
| Video tutorials / screencasts | Expensive to produce, impossible to update in place, SEO/search unfriendly. |
| Public Discord/Slack community at launch | Pre-1.0 support burden. GitHub Issues + Discussions only. |
| "Awesome Zeroth" curated extension list | No ecosystem exists yet — would look abandoned. |
| Core/platform file-level split | Superseded by the pure-rename decision — the cascading `__init__.py` breakage isn't worth it. |
| Monorepo consolidation of `zeroth-core` + `zeroth-studio` | Directly contradicts the intentional split decision. |
| Runtime feature work (new subsystems, new integrations) | v3.0 is packaging and docs only. New runtime work goes to v3.1+. |

## Traceability

Which phases cover which requirements. Updated after roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| PKG-01 | Phase 28 | Pending |
| PKG-02 | Phase 28 | Pending |
| PKG-03 | Phase 28 | Pending |
| PKG-04 | Phase 28 | Pending |
| PKG-05 | Phase 28 | Pending |
| PKG-06 | Phase 28 | Pending |
| RENAME-01 | Phase 27 | Pending |
| RENAME-02 | Phase 27 | Pending |
| RENAME-03 | Phase 27 | Pending |
| RENAME-04 | Phase 27 | Pending |
| RENAME-05 | Phase 27 | Pending |
| DOCS-01 | Phase 30 | Complete |
| DOCS-02 | Phase 30 | Complete |
| DOCS-03 | Phase 31 | Complete |
| DOCS-04 | Phase 31 | Complete |
| DOCS-05 | Phase 30 | Complete |
| DOCS-06 | Phase 31 | Complete |
| DOCS-07 | Phase 32 | Complete |
| DOCS-08 | Phase 32 | Complete |
| DOCS-09 | Phase 32 | Complete |
| DOCS-10 | Phase 32 | Complete |
| DOCS-11 | Phase 32 | Complete |
| DOCS-12 | Phase 31 | Complete |
| SITE-01 | Phase 30 | Complete |
| SITE-02 | Phase 30 | Complete |
| SITE-03 | Phase 30 | Pending |
| SITE-04 | Phase 30 | Complete |
| STUDIO-01 | Phase 29 | Complete |
| STUDIO-02 | Phase 29 | Complete |
| STUDIO-03 | Phase 29 | Complete |
| STUDIO-04 | Phase 29 | Complete |
| STUDIO-05 | Phase 29 | Complete |
| ARCHIVE-01 | Phase 27 | Pending |
| ARCHIVE-02 | Phase 27 | Pending |
| ARCHIVE-03 | Phase 27 | Pending |

| Requirement | Phase | Status |
|-------------|-------|--------|
| HTTP-01 | Phase 35 | Complete |
| HTTP-02 | Phase 35 | Complete |
| HTTP-03 | Phase 35 | Complete |
| HTTP-04 | Phase 35 | Complete |
| HTTP-05 | Phase 35 | Complete |
| HTTP-06 | Phase 35 | Complete |
| TMPL-01 | Phase 36 | Complete |
| TMPL-02 | Phase 36 | Complete |
| TMPL-03 | Phase 36 | Complete |
| TMPL-04 | Phase 36 | Complete |
| CTXW-01 | Phase 37 | Complete |
| CTXW-02 | Phase 37 | Complete |
| CTXW-03 | Phase 37 | Complete |
| CTXW-04 | Phase 37 | Complete |
| CTXW-05 | Phase 37 | Complete |
| PARA-01 | Phase 38 | Complete |
| PARA-02 | Phase 38 | Complete |
| PARA-03 | Phase 38 | Complete |
| PARA-04 | Phase 38 | Complete |
| PARA-05 | Phase 38 | Complete |
| PARA-06 | Phase 38 | Complete |
| SUBG-01 | Phase 39 | Complete |
| SUBG-02 | Phase 39 | Complete |
| SUBG-03 | Phase 39 | Complete |
| SUBG-04 | Phase 39 | Complete |
| SUBG-05 | Phase 39 | Complete |
| SUBG-06 | Phase 39 | Complete |
| SUBG-07 | Phase 39 | Complete |
| SUBG-08 | Phase 39 | Complete |
| D-01 | Phase 41 | Complete |
| D-02 | Phase 41 | Complete |
| D-03 | Phase 41 | Complete |
| D-04 | Phase 42 | Complete |
| D-05 | Phase 41 | Complete |
| D-06 | Phase 41 | Complete |
| D-07 | Phase 41 | Complete |

### v4.1 Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| ORCH-01 | Phase 43 | Pending |
| ORCH-02 | Phase 43 | Pending |
| ORCH-03 | Phase 43 | Pending |
| ORCH-04 | Phase 43 | Pending |
| TMB-01 | Phase 44 | Complete |
| TMB-02 | Phase 44 | Complete |
| TMB-03 | Phase 44 | Complete |
| TMB-04 | Phase 44 | Complete |
| TMB-05 | Phase 44 | Complete |
| TMB-06 | Phase 44 | Complete |
| ARTS-01 | Phase 45 | Pending |
| ARTS-02 | Phase 45 | Pending |
| ARTS-03 | Phase 45 | Pending |
| ARTS-04 | Phase 45 | Pending |
| TREG-01 | Phase 46 | Pending |
| TREG-02 | Phase 46 | Pending |
| TREG-03 | Phase 46 | Pending |
| CBRK-01 | Phase 47 | Pending |
| CBRK-02 | Phase 46 | Pending |
| CBRK-03 | Phase 46 | Pending |

**v4.1 Coverage:**
- v4.1 requirements: 14 total
- Mapped to phases: 14
- Unmapped: 0

**Coverage:**
- v3.0 requirements: 34 total
- Mapped to phases: 34
- Unmapped: 0
- v4.0 requirements: 36 total (all Complete)
- Mapped to phases: 36
- Unmapped: 0

---

## v2.0 Requirements (Zeroth Studio — partially shipped)

Phases 22–23 shipped in v2.0. Phases 24–26 deferred to the new `zeroth-studio` repo — see "Deferred to `zeroth-studio` repo" above. v2.0 REQ-IDs are preserved for history but not re-mapped in v3.0.

### Canvas Foundation

- [x] **CANV-01** · [x] **CANV-02** · [x] **CANV-03** · [x] **CANV-04** · [x] **CANV-05** · [x] **CANV-06** · [x] **CANV-07** · [x] **CANV-08** · [x] **CANV-09** · [x] **CANV-10**

### Graph Authoring API

- [x] **API-01** · [x] **API-03** · [ ] **API-02** (→ zeroth-studio) · [ ] **API-04** (→ zeroth-studio)

### Governance Visualization

- [ ] **GOV-01** … **GOV-07** (→ zeroth-studio)

### AI Authoring

- [ ] **AUTH-01** … **AUTH-05** (→ zeroth-studio) · [x] **AUTH-06**

### Versioning & Collaboration

- [ ] **COLLAB-01** · [ ] **COLLAB-02** (→ zeroth-studio)

### Deployment & Infrastructure

- [x] **INFRA-01** · [x] **INFRA-02**

---

*Requirements last updated: 2026-06-20 (MBND + SAFE + OBS + PRES + RAG + EVAL shipped — all six v4.2 candidate gaps closed; semantic response caching and baseline-diff eval regression remain deferred).*

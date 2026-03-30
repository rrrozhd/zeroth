# Zeroth — Progress

`[ ]` not started · `[~]` in progress · `[x]` done · `[-]` blocked/deferred

Detailed requirements for each task: `phases/phase-N-*/PLAN.md`
Artifacts and evidence: `phases/phase-N-*/artifacts/`

---

## Phase 1 — Core Foundation

> All 1A–1D parallel. 1E/1F depend on 1A–1C, can overlap with Phase 2 start.

### 1A. Domain Models & Graph Schema `phases/phase-1-foundation/PLAN.md`

- [x] Define canonical Graph model
- [x] Define Node model (discriminated union)
- [x] Define agent-specific node data
- [x] Define executable-unit-specific node data
- [x] Define human-approval-specific node data
- [x] Define Edge model
- [x] Define Condition model
- [x] Define graph execution settings
- [x] Implement graph serialization layer
- [x] Implement graph storage schema with migration version
- [x] Support draft / published / archived lifecycle
> **Note:** Graph definitions compile toward GovernAI `GovernedFlowSpec`/`GovernedStepSpec`/`TransitionSpec`, with later-phase runtime bindings stored as plain refs for contracts, tools, policies, and memory.
- [x] **Artifact:** serialization round-trip tests pass
- [x] **Artifact:** storage schema migration applied
- [x] **Artifact:** lifecycle transition tests pass

### 1B. Contract & Validation Registry

- [x] Implement contract registry
- [x] Support nested types, required/optional, enums, arrays, metadata
- [x] Implement model versioning and reference system
- [x] Implement model version resolver
> **Note:** Registry stores typed Pydantic contract metadata and adds GovernAI tool/step adapters instead of duplicating GovernAI runtime abstractions.
- [x] **Artifact:** registry CRUD tests pass
- [x] **Artifact:** complex model tests pass (nested, enums, arrays)
- [x] **Artifact:** version resolution tests pass

### 1C. Edge Mapping System

- [x] Define mapping schema
- [x] Implement mapping executor
- [x] Implement mapping validator
- [x] Persist mappings on edge definitions
- [x] **Artifact:** per-operation-type tests pass
- [x] **Artifact:** validation rejection tests pass
- [x] **Artifact:** edge persistence round-trip tests pass

### 1D. Run & Thread Models

- [x] Define Run model
- [x] Define Thread model
- [x] Define run result states enum
- [x] Implement run state schema and persistence
- [x] Implement thread model and repository
- [x] **Artifact:** run CRUD and transition tests pass
- [x] **Artifact:** thread create/continue tests pass
> **Note:** `Run` now subclasses GovernAI `RunState`; Zeroth stores graph/deployment metadata and mirrors GovernAI-style checkpoint and thread-index behavior instead of inventing a conflicting core run model.

### 1E. Graph Validation Engine

- [x] Implement all validation rules (node IDs, edges, conditions, contracts, mappings, entrypoint, tools, memory, loops, approvals, policies)
- [x] Cyclic graphs accepted with safeguards
- [x] Cyclic graphs rejected without safeguards
- [x] Structured validation report with warning/error taxonomy
> **Note:** Validation is intentionally shape/reference based for unresolved later-phase registries while still enforcing loop safeguards, attachment presence, and structured issue taxonomy.
- [x] **Artifact:** per-rule unit tests pass
- [x] **Artifact:** combined scenario tests pass

### 1F. Graph CRUD & Versioning

- [x] Implement graph creation and editing
- [x] Implement publishing (immutable)
- [x] Implement clone published → draft
- [x] Implement archival
- [x] Implement version history queries
- [x] Implement graph diff engine
> **Note:** Published graph versions remain immutable; new editable revisions are created through `clone_published_to_draft`, and diffs classify node, edge, condition, contract, policy, memory, and executable-unit binding changes separately.
- [x] **Artifact:** full lifecycle tests pass
- [x] **Artifact:** diff engine tests pass

### Phase 1 Gate

- [x] All domain models defined and stored
- [x] Serialization round-trips verified
- [x] Contract registry operational
- [x] Edge mapping executor and validator operational
- [x] Run and thread models persisted
- [x] Graph validation produces structured reports
- [x] Graph CRUD supports full lifecycle with versioning
- [x] All unit tests pass

---

## Phase 2 — Execution Core

> Stream A (2A→2B/2C→2D), Stream B (2E→2F), Stream C (2G) — all parallel.
> 2H converges all streams. 2I parallel with 2H.

### 2A. EU Manifest & Onboarding `phases/phase-2-execution/PLAN.md`

- [x] Implement EU manifest schema
- [x] Implement manifest validator
- [x] Implement Mode A — Native Unit
- [x] Implement Mode B — Wrapped Command Unit
- [x] Implement Mode C — Project Unit
- [x] Implement minimal-adaptation wrapping
> **Note:** Zeroth manifests now describe GovernAI-backed executable units and materialize into `PythonTool` or CLI-tool adapters instead of introducing a separate execution abstraction.
- [x] **Artifact:** manifest validation tests pass
- [x] **Artifact:** all three mode onboarding tests pass

### 2B. Input/Output Modes

- [x] Implement input injection modes (json_stdin, cli_args, env_vars, input_file_json)
- [x] Implement output extraction modes (json_stdout, tagged_stdout_json, output_file_json, text_stdout, exit_code_only)
- [x] Implement output-to-typed-node-output conversion
- [x] Implement extraction validators
- [x] **Artifact:** per-mode injection/extraction tests pass

### 2C. Language Runtime Adapters

- [x] Define runtime adapter interface
- [x] Implement Python adapter
- [x] Implement generic command adapter
- [x] **Artifact:** adapter tests pass (Python inline, command wrapping)

### 2D. Sandbox Execution

- [x] Implement sandbox manager
- [x] Implement sandbox lifecycle controller
- [x] Implement environment builder
- [x] Implement environment cache manager
- [x] Validate cache policy isolation
- [x] **Artifact:** isolation tests pass
- [x] **Artifact:** cache hit/miss tests pass
> **Note:** Sandbox foundations are implemented as a local subprocess/tempdir backend with explicit env allowlisting, cache-key helpers, and in-memory environment snapshots for later runner integration.

### 2E. Agent Runtime

- [x] Implement agent config model
- [x] Implement agent runner
- [x] Implement model/provider invocation adapter
- [x] Implement output validation wrapper
- [x] Implement prompt/instruction assembly
> **Note:** The Zeroth agent runtime wraps GovernAI-compatible provider and prompt concepts, with deterministic provider adapters and in-memory thread-state checkpointing for testability.
- [x] **Artifact:** agent runner lifecycle tests pass
- [x] **Artifact:** prompt assembly tests pass
- [x] **Artifact:** retry/timeout tests pass

### 2F. Tool Attachment

- [x] Implement tool attachment manifest
- [x] Implement agent-to-tool bridge
- [x] Implement tool permission wrapper
- [x] Enforce no undeclared tools
- [x] Ensure tool call auditability
- [x] **Artifact:** tool bridge tests pass
- [x] **Artifact:** permission enforcement tests pass

### 2G. Condition & Branch Resolution

- [x] Implement condition schema and evaluator
- [x] Implement condition binding system
- [x] Implement branch resolver
- [x] Implement next-step planner
- [x] Implement condition result recorder
> **Note:** Condition outcomes are deterministic, AST-based, and recorded directly as `RunConditionResult` entries on Zeroth run state.
- [x] **Artifact:** evaluator tests pass
- [x] **Artifact:** branch resolution pattern tests pass
- [x] **Artifact:** cycle traversal tests pass

### 2H. Runtime Orchestrator

- [x] Implement async run orchestrator
- [x] Implement node dispatcher
- [x] Implement run lifecycle manager
- [x] Implement loop/cycle safeguards
- [x] Implement run state transition manager
> **Note:** The orchestrator persists node payload queues, edge-visit counts, audits, and checkpoints directly on Zeroth run state, while approval nodes pause with a stub metadata payload until the Phase 3 approval API lands.
- [x] **Artifact:** linear graph execution test passes
- [x] **Artifact:** cyclic graph execution test passes
- [x] **Artifact:** conditional branching test passes
- [x] **Artifact:** loop guard tests pass
- [x] **Artifact:** state persistence/recovery test passes

### 2I. Thread Checkpoint/Restore

- [x] Implement thread lifecycle
- [x] Implement thread resolver
- [x] Implement state checkpoint interface
- [x] Implement thread-scoped state store
- [x] Support stateless agent no-op
- [x] Separate checkpoints from audits
> **Note:** Thread state snapshots are persisted through repository-backed run checkpoints with a dedicated `thread_state` marker, so restore remains separate from node audit payloads and does not add synthetic run history entries.
- [x] **Artifact:** thread create/continue tests pass
- [x] **Artifact:** checkpoint/restore tests pass
- [x] **Artifact:** cross-run continuity test passes

### Phase 2 Gate

- [x] Orchestrator runs simple graph end-to-end (agent + EU + conditions)
- [x] Executable units run in sandbox
- [x] Thread checkpoint/restore works for stateful agents
- [x] All stream tests pass

---

## Phase 3 — Platform Control

> 3A, 3B→3C, 3D, 3E — all parallel. All depend on Phase 2 but not each other.

### 3A. Memory Connector System `phases/phase-3-control/PLAN.md`

- [x] Define memory connector interface
- [x] Implement connector manifest model and registry
- [x] Implement shared memory instance binding
- [x] Implement connector instance resolver
- [x] Implement memory access recorder
- [x] Implement MVP connector: ephemeral run-scoped memory
- [x] Implement MVP connector: key-value memory
- [x] Implement MVP connector: conversation/thread memory
- [x] **Artifact:** shared memory binding tests pass
- [x] **Artifact:** all 3 connector implementation tests pass
- [x] **Artifact:** memory access audit recording tests pass
- [x] **Artifact:** integration tests pass

### 3B. Human Approval Node

- [x] Implement approval node runner
- [x] Implement approval interaction model
- [x] Support decision modes (approve, reject, edit_and_approve)
- [x] Implement resolution validator
- [x] Implement async continuation after resolution
- [x] Implement approval audit integration
- [x] Implement human-facing approval payload contract
- [x] Implement redaction-aware approval serializer
- [x] Preserve extensibility for future interaction types
- [x] **Artifact:** approval lifecycle tests pass (all 3 decision modes)
- [x] **Artifact:** paused state persistence tests pass
- [x] **Artifact:** resolution validation tests pass
- [x] **Artifact:** redaction tests pass

### 3C. Approval API

- [x] Implement approval query API
- [x] Implement approval context fetch
- [x] Implement approval resolution API
- [x] Implement approver authorization validation
- [x] Implement edited payload validation
- [x] Trigger async continuation
- [x] Prevent duplicate resolutions (idempotency)
- [x] Record all API actions in audit
- [x] **Artifact:** query API tests pass
- [x] **Artifact:** resolution API tests pass
- [x] **Artifact:** idempotency tests pass
- [x] **Artifact:** audit recording tests pass

### 3D. Audit System

- [x] Implement node audit record schema
- [x] Implement audit repository and write pipeline
- [x] Implement audit query interfaces
- [x] Implement audit timeline assembler
- [x] Implement redaction policy layer
- [x] Implement payload sanitizer and config model
- [x] **Artifact:** write pipeline tests pass
- [x] **Artifact:** query scope tests pass (node, run, graph, deployment, thread)
- [x] **Artifact:** redaction/sanitizer tests pass
- [x] **Artifact:** full-run audit trail integration test passes

### 3E. Policy & Capability Enforcement

- [x] Define capability schema
- [x] Implement capability manifest bindings
- [x] Implement policy schema
- [x] Implement policy binding model (graph + node level)
- [x] Implement policy evaluation module
- [x] Implement runtime policy guard
- [x] Implement policy-aware sandbox enforcement
- [x] Produce audit entries on violations
- [x] **Artifact:** policy evaluation tests pass (each type)
- [x] **Artifact:** sandbox enforcement tests pass
- [x] **Artifact:** violation audit tests pass
- [x] **Artifact:** integration test: policy block + audit log

### Phase 3 Gate

- [x] Memory-equipped agents work with shared connectors
- [x] Approval pause/resume works end-to-end
- [x] Approval API fully operational
- [x] Audit trail present on every node execution
- [x] Policy violations blocked and logged
- [x] All stream tests pass

---

## Phase 4 — Deployment Surface

> Sequential: 4A → 4B → then 4C/4D/4E parallel.

### 4A. Published Graph Deployment `phases/phase-4-deployment/PLAN.md`

- [x] Implement deployment model
- [x] Implement publish-to-deploy workflow
- [x] Implement rollback mechanism
- [x] **Artifact:** publish-to-deploy tests pass
- [x] **Artifact:** immutability enforcement tests pass
- [x] **Artifact:** rollback tests pass
> **Note:** Deployment snapshots now resolve the latest published graph version for unversioned deploys and keep each `deployment_ref` bound to a single graph lineage.

### 4B. Service Wrapper

- [x] Implement service wrapper runtime
- [x] Implement run status retrieval endpoint
- [x] Implement contract metadata exposure
- [x] Implement approval resolution routes
- [x] Implement health endpoint
- [x] Implement deployment bootstrapper
- [x] **Artifact:** all endpoint tests pass
- [x] **Artifact:** bootstrapper tests pass
> **Note:** The original 4B slice stopped at app/bootstrap wiring and `/health`; run status, contract metadata, and approval routes are now complete through the later Phase 4 service tasks.

### 4C. Async Invocation & Run API

- [x] Implement run creation API
- [x] Implement run status API
- [x] Implement terminal/nonterminal response models
- [x] **Artifact:** run creation tests pass (valid + invalid)
- [x] **Artifact:** thread_id handling tests pass
- [x] **Artifact:** run status tests pass (all states)
> **Note:** The Phase 4 MVP dispatches runs in-process with bounded background tasks and shutdown draining; durable job supervision remains outside 4C.

### 4D. Public API Contract Exposure

- [x] Implement input contract endpoint
- [x] Implement output contract endpoint
- [x] Implement error/result state schema endpoint
- [x] Implement deployment version metadata endpoint
- [x] **Artifact:** all contract exposure endpoint tests pass

### 4E. Thread-Aware External API

- [x] Implement invocation contract with optional thread_id
- [x] Implement thread-aware runtime resolver
- [x] Ensure thread linkage in run state and audits
- [x] **Artifact:** new thread creation tests pass
- [x] **Artifact:** thread continuation tests pass
- [x] **Artifact:** thread linkage visibility tests pass
> **Note:** Omitted `thread_id` still creates a fresh thread automatically. An explicit `thread_id` is validated against the active deployment snapshot when that thread already exists; a brand-new explicit key is currently accepted as a new thread context for API compatibility.

### Phase 4 Gate

- [x] Deployed graph callable via HTTP API
- [x] Async run creation and status retrieval work
- [x] Approval resolution works via API
- [x] Thread continuity works across API invocations
- [x] Contract metadata exposed
- [x] Health endpoint responds
- [x] All endpoint tests pass

---

## Phase 5 — Integration & Polish

> 5A and 5B parallel.

### 5A. End-to-End Integration Tests `phases/phase-5-integration/PLAN.md`

- [x] Test 1: linear graph (agent → EU → agent)
- [x] Test 2: cyclic graph with loop guard termination
- [x] Test 3: conditional branching with fan-out
- [x] Test 4: human approval pause and resume
- [x] Test 5: thread continuity across multiple runs
- [x] Test 6: shared memory connector between agents
- [x] Test 7: deploy graph and invoke via service wrapper API
- [x] Test 8: policy violation produces audit entry and fails execution

### 5B. Specification Documents

- [x] Executable Unit Manifest Spec
- [x] Runtime Execution Semantics Spec
- [x] Thread and State Persistence Spec
- [x] Public API Spec
- [x] Audit Record Spec

### Phase 5 Gate

- [x] All 8 integration tests pass
- [x] All 5 specification documents written and reviewed
- [x] MVP shippable

---

## Post-MVP Roadmap

> Phases 6–9 close the remaining gap between the current MVP and Zeroth's intended governed, security-first, transparency-first production platform.

## Phase 6 — Identity & Tenant Governance

> 6A → 6B → 6C is the critical path. 6D can overlap once 6A/6B contracts stabilize.

### 6A. Service Authentication & Principal Context `phases/phase-6-identity-governance/PLAN.md`

- [x] Introduce an authenticated principal model for public and internal service calls
- [x] Support API key and bearer-token auth adapters behind one auth interface
- [x] Remove caller-supplied approval identity from the public resolution API
- [x] Propagate principal context into runs, approvals, and audits
- [x] **Artifact:** authentication middleware tests pass
- [x] **Artifact:** approval attribution tests pass

### 6B. Authorization & Role Model

- [x] Define roles and permission vocabulary for deployment, run, approval, and audit access
- [x] Enforce authorization checks on all service routes and sensitive repository queries
- [x] Support least-privilege operator, reviewer, and admin flows
- [x] **Artifact:** RBAC matrix tests pass
- [x] **Artifact:** unauthorized access tests pass

### 6C. Tenant / Workspace Isolation

- [x] Add tenant/workspace scope to deployments, runs, threads, approvals, and audits
- [x] Enforce tenant-scoped lookup, list, and continuation behavior
- [x] Reject and audit cross-tenant access attempts
- [x] **Artifact:** tenant isolation integration tests pass
- [x] **Artifact:** cross-tenant rejection tests pass

### 6D. Governance Identity Surfaces

- [x] Expose submitter, approver, and tenant lineage in external run and approval metadata
- [x] Record actor lineage for approval and policy decisions in audits
- [x] Document the identity and access model
- [x] **Artifact:** public contract tests pass
- [x] **Artifact:** governance attribution end-to-end test passes

### Phase 6 Gate

- [x] Every external request is authenticated
- [x] Approval actions are attributed to authenticated principals only
- [x] Cross-tenant access is blocked and auditable
- [x] All identity and access tests pass

---

## Phase 7 — Transparent Governance & Verifiable Provenance

> 7A and 7B can run in parallel. 7C and 7D depend on the audit and deployment payload shape settling.

### 7A. Public Audit & Timeline API `phases/phase-7-transparent-governance/PLAN.md`

- [x] Implement public audit query endpoints scoped by run, thread, deployment, and graph version
- [x] Implement audit timeline exposure for run-level and deployment-level review
- [x] Apply authorization and redaction rules to audit reads
- [x] **Artifact:** audit API tests pass
- [x] **Artifact:** timeline query tests pass

### 7B. Governance Evidence Bundles

- [x] Expose stable run-to-evidence and deployment-to-evidence bundle references
- [x] Export review-friendly evidence packages for runs and deployments
- [x] Include policy, tool, approval, and memory lineage in exported evidence
- [x] **Artifact:** evidence bundle tests pass
- [x] **Artifact:** export contract tests pass

### 7C. Tamper-Evident Audit Trail

- [x] Replace mutable audit upserts with append-only or tamper-evident persistence
- [x] Add audit continuity verification for run and deployment history
- [x] Define correction/supersession semantics without rewriting history
- [x] **Artifact:** tamper-detection tests pass
- [x] **Artifact:** append-only audit tests pass

### 7D. Deployment Provenance & Attestations

- [x] Add deployment snapshot digests for graph and pinned contract state
- [x] Produce deployment attestation payloads for external verification
- [x] Expose verification helpers or APIs for attested deployments
- [x] **Artifact:** attestation generation tests pass
- [x] **Artifact:** deployment verification tests pass

### Phase 7 Gate

- [x] Audits are externally queryable through the service surface
- [x] Run and deployment evidence can be exported and reviewed independently
- [x] Audit mutation is detectable or prevented by design
- [x] Deployment provenance is verifiable

---

## Phase 8 — Runtime Security Hardening

> 8A and 8B are the critical path. 8C overlaps once the secret/provider abstraction is chosen. 8D can follow the hardened execution path.

### 8A. Hardened Sandbox Backend `phases/phase-8-runtime-security/PLAN.md`

- [~] Make a hardened sandbox backend the default for untrusted executable units
- [x] Remove silent fallback to permissive host-local execution for isolated workloads
- [x] Enforce resource ceilings and filesystem boundaries
- [x] **Artifact:** hardened sandbox tests pass
- [x] **Artifact:** isolation regression tests pass

### 8B. Policy-Derived Runtime Enforcement

- [x] Enforce network, timeout, secret, and side-effect constraints from policy results
- [x] Gate policy-required side effects behind approval when configured
- [x] Ensure retries, resumes, and background execution preserve policy constraints
- [x] **Artifact:** runtime policy enforcement tests pass
- [x] **Artifact:** side-effect approval tests pass

### 8C. Secret Management & Data Protection

- [x] Replace raw runtime secrets with secret references and a provider abstraction
- [x] Protect sensitive data at rest for local persistence layers
- [x] Verify that checkpoints, approvals, and audits do not retain secret material
- [x] **Artifact:** secret provider tests pass
- [x] **Artifact:** secret-leak regression tests pass

### 8D. Executable-Unit Integrity & Admission Control

- [x] Add digests or signed metadata for executable-unit manifests and artifacts
- [x] Validate allowed runtimes, images, or commands before execution
- [x] Reject untrusted executable-unit definitions and audit the reason
- [x] **Artifact:** executable-unit integrity tests pass
- [x] **Artifact:** admission-control tests pass

### Phase 8 Gate

- [~] Untrusted executable units no longer rely on permissive local execution by default
- [x] Policy constraints are enforced in runtime behavior, not only logged
- [x] Secrets are reference-based and protected across runtime and persistence
- [x] Executable-unit integrity is checked before execution

---

## Phase 9 — Durable Control Plane & Production Operations

> 9A is foundational. 9B depends on 9A. 9C and 9D can overlap once durable dispatch exists.

### 9A. Durable Run Dispatch & Worker Supervision `phases/phase-9-durable-control-plane/PLAN.md`

- [x] Replace in-process background dispatch with durable queue or lease-based execution ownership
- [x] Separate API request handling from worker execution lifecycle
- [x] Preserve idempotency across retries and duplicate submissions
- [x] **Artifact:** durable dispatch tests pass (`tests/dispatch/test_lease.py`, `test_worker.py`)
- [x] **Artifact:** worker retry/idempotency tests pass (`tests/service/test_durable_dispatch.py`)

### 9B. Resume & Recovery Semantics

- [x] Make approvals, retries, and thread continuation restart-safe
- [x] Recover safely from worker crashes and partial completion
- [x] Avoid duplicated side effects during recovery
- [x] **Artifact:** restart recovery tests pass (`tests/dispatch/test_recovery.py`)
- [x] **Artifact:** approval recovery tests pass (`tests/dispatch/test_recovery.py`)

### 9C. Operational Guardrails

- [x] Add rate limiting, quotas, and bounded concurrency per tenant or deployment
- [x] Surface backpressure instead of accepting unbounded work
- [x] Add dead-letter or operator-review handling for repeated failures
- [x] **Artifact:** rate limit and quota tests pass (`tests/guardrails/test_rate_limit.py`)
- [x] **Artifact:** backpressure tests pass (`tests/service/test_guardrails_api.py`)

### 9D. Observability & Admin Controls

- [x] Add metrics for queue depth, run latency, approval wait time, and worker failures
- [x] Add tracing or correlation metadata across API, orchestrator, approvals, and audits
- [x] Add administrative controls for interruption, cancellation, and replay-safe inspection
- [x] **Artifact:** observability contract tests pass (`tests/observability/`)
- [x] **Artifact:** admin control integration tests pass (`tests/service/test_admin_api.py`)

### Phase 9 Gate

- [x] Run execution survives service restarts without losing work
- [x] Recovery paths preserve approval and thread correctness
- [x] Capacity guardrails protect the platform under load
- [x] Operators can observe and control the runtime safely

---

## Log

### 2026-03-28 15:07 — Phase 9 review follow-up: defects found in branch tip
**Phase/Tasks:** 9A, 9C, 9D
**Status:** in-progress
**What:** Reviewed branch `claude/plan-phase-9-t5IMd` and found durable-dispatch ownership bugs, admin/metrics authorization gaps, and a broken local `governai` dependency path in `pyproject.toml` that prevents local `uv` verification in this environment.
**Tests:** `uv run pytest -q tests/dispatch tests/guardrails tests/observability tests/service/test_admin_api.py tests/service/test_durable_dispatch.py tests/service/test_guardrails_api.py tests/service/test_metrics_endpoint.py` failed with `Distribution not found at: file:///home/user/governai`; `uv run ruff check src tests` failed with the same dependency resolution error
**Artifacts:** none
**Blockers:** branch tip references a nonexistent `governai` path and phase 9 regressions need fixes before the completion claim is trustworthy
**Next:** add regression tests for the reviewed defects, patch the branch, and rerun the smallest viable verification set

### 2026-03-28 15:12 — Phase 9 review follow-up: verification environment restored
**Phase/Tasks:** 9A, 9C, 9D
**Status:** in-progress
**What:** Restored the workspace-local `governai` dependency path in `pyproject.toml` and reran `uv sync` so phase 9 regression tests can run in the intended environment.
**Tests:** `uv sync` passed
**Artifacts:** none
**Blockers:** branch correctness issues still need regression tests and fixes
**Next:** write failing tests for the reviewed defects, then patch the implementation

### 2026-03-28 15:18 — Phase 9 review follow-up: targeted regression tests added
**Phase/Tasks:** 9A, 9D
**Status:** in-progress
**What:** Added red tests covering worker over-claiming beyond available concurrency, Prometheus counter rendering, admin lease clearing, admin/metrics scope enforcement, metrics permission checks, and interrupt status serialization.
**Tests:** `uv run pytest -q tests/dispatch/test_worker.py tests/observability/test_metrics.py tests/service/test_admin_api.py tests/service/test_metrics_endpoint.py` failed with 10 expected regressions across worker leasing, admin scope, metrics naming, metrics access, and interrupt status
**Artifacts:** none
**Blockers:** implementation still allows the reviewed regressions
**Next:** patch the worker, admin API, metrics rendering, and public run-status mapping until the new tests turn green

### 2026-03-28 15:24 — Phase 9 review follow-up: targeted regressions fixed
**Phase/Tasks:** 9A, 9D
**Status:** in-progress
**What:** Patched the durable worker to reserve concurrency before claiming new work, added unconditional lease clearing for admin cancel, enforced deployment scope and metrics permission checks on admin/metrics routes, fixed public serialization for `WAITING_INTERRUPT`, and corrected Prometheus counter rendering so existing metric names are not suffixed twice.
**Tests:** `uv run pytest -q tests/dispatch/test_worker.py tests/observability/test_metrics.py tests/service/test_admin_api.py tests/service/test_metrics_endpoint.py` passed (27 passed)
**Artifacts:** none
**Blockers:** broader phase 9 verification and lint still need to run before the branch can be rewritten confidently
**Next:** run the wider phase 9 test slice and lint, then assess whether any reviewed issues remain

### 2026-03-28 15:33 — Phase 9 review follow-up: wider verification passed, full suite found one contract drift
**Phase/Tasks:** 9A, 9D
**Status:** in-progress
**What:** Cleaned up the branch test style debt flagged by `ruff`, reran the broader phase 9 slice successfully, then ran the full suite and found one remaining contract test expectation that needed to include the new public `waiting_interrupt` status.
**Tests:** `uv run ruff check src tests` passed; `uv run pytest -q tests/dispatch tests/guardrails tests/observability tests/service/test_admin_api.py tests/service/test_durable_dispatch.py tests/service/test_guardrails_api.py tests/service/test_metrics_endpoint.py` passed (59 passed); `uv run pytest -q` failed with 1 contract schema assertion in `tests/service/test_contract_api.py`
**Artifacts:** none
**Blockers:** full-suite verification still needs one rerun after updating the contract schema expectation
**Next:** rerun the full suite and, if green, rewrite the phase 9 branch commit and push

### 2026-03-28 15:40 — Phase 9 review follow-up: branch corrected and fully reverified
**Phase/Tasks:** 9A, 9D
**Status:** completed
**What:** Completed a review-driven correction pass on the phase 9 branch. Fixed worker over-claiming against bounded concurrency, restored lease clearing on admin cancel, enforced scope and metrics permission checks on admin/metrics routes, exposed `waiting_interrupt` as a first-class public run status, corrected Prometheus counter rendering, repaired the local `governai` dependency path, and updated the affected regression and contract tests.
**Tests:** `uv run ruff check src tests` passed; `uv run pytest -q tests/dispatch tests/guardrails tests/observability tests/service/test_admin_api.py tests/service/test_durable_dispatch.py tests/service/test_guardrails_api.py tests/service/test_metrics_endpoint.py` passed (59 passed); `uv run pytest -q` passed (280 passed)
**Artifacts:** none
**Blockers:** none
**Next:** rewrite the branch tip commit with the reviewed fixes and push the updated branch

### 2026-03-27 21:10 — Phase 9 complete: durable control plane and production operations
**Phase/Tasks:** 9A, 9B, 9C, 9D
**Status:** completed
**What:** Replaced fragile asyncio background dispatch with a SQLite-backed lease queue. Added 54 new tests across `tests/dispatch/`, `tests/guardrails/`, `tests/observability/`, and 4 new `tests/service/` files. Fixed 15+ GovernAI stub incompatibilities, uppercase `RunStatus` values, approval continuation scheduling, and correlation ID propagation. All 275 tests pass, lint clean.
**Tests:** `uv run pytest` → 275 passed, 0 failed
**Artifacts:** `tests/dispatch/test_lease.py`, `test_worker.py`, `test_recovery.py`; `tests/guardrails/test_rate_limit.py`, `test_dead_letter.py`; `tests/observability/test_metrics.py`, `test_correlation.py`; `tests/service/test_guardrails_api.py`, `test_metrics_endpoint.py`, `test_admin_api.py`, `test_durable_dispatch.py`
**Blockers:** none
**Next:** Phase 10 or deployment packaging

### 2026-03-27 12:05 — Added post-MVP roadmap phases 6 through 9
**Phase/Tasks:** 6A-9D roadmap
**Status:** completed
**What:** Extended the root roadmap with post-MVP phases covering identity and tenant governance, transparent governance and provenance, runtime security hardening, and a durable control plane. Added phase plan documents under `phases/phase-6-identity-governance/PLAN.md`, `phases/phase-7-transparent-governance/PLAN.md`, `phases/phase-8-runtime-security/PLAN.md`, and `phases/phase-9-durable-control-plane/PLAN.md`, plus placeholder `artifacts/` directories for each phase.
**Tests:** not run
**Artifacts:** none
**Blockers:** none
**Next:** Choose Phase 6 as the first execution target, then break its streams into implementation tasks and verification artifacts.

### 2026-03-27 12:05 — Phase 6 auth red baseline
**Phase/Tasks:** 6A
**Status:** in-progress
**What:** added the first Phase 6 auth test slice in `tests/service/test_auth_api.py` and extended `tests/service/helpers.py` with service-auth test helpers so the new service authentication boundary can be implemented test-first.
**Tests:** `uv run pytest -v tests/service/test_auth_api.py` failed with `ModuleNotFoundError: No module named 'zeroth.service.auth'`
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6a-auth-red-2026-03-27.txt`
**Blockers:** service auth/principal modules and bootstrap wiring do not exist yet
**Next:** implement the shared identity models, auth config, bootstrap wiring, and auth middleware to make the new auth tests pass

### 2026-03-27 12:09 — Phase 6 API-key auth foundation
**Phase/Tasks:** 6A
**Status:** in-progress
**What:** added shared identity models under `src/zeroth/identity/`, implemented API-key authentication and bearer-config scaffolding in `src/zeroth/service/auth.py`, added request auth middleware and bootstrap wiring, removed request-body approver trust from `src/zeroth/service/approval_api.py`, and switched approval resolution storage to structured actor identity in the approvals and audit models.
**Tests:** `uv run pytest -v tests/service/test_auth_api.py` passed
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6a-auth-green-2026-03-27.txt`
**Blockers:** bearer/JWT verification, route-wide RBAC enforcement, and tenant/workspace persistence are still pending
**Next:** add the bearer/JWT red tests, finish route authorization, and then add tenant/workspace storage and isolation enforcement

### 2026-03-27 12:11 — Phase 6 RBAC service gates
**Phase/Tasks:** 6B
**Status:** in-progress
**What:** added route-level permission enforcement in the run, contract, and approval APIs, defined the Phase 6 permission vocabulary and role matrix in `src/zeroth/service/authorization.py`, and started denial auditing through the existing audit repository using synthetic service node IDs.
**Tests:** `uv run pytest -v tests/service/test_rbac_api.py` passed
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6b-rbac-green-2026-03-27.txt`
**Blockers:** remaining service routes, tenant/workspace persistence, and JWT bearer verification are still pending
**Next:** add tenant/workspace isolation tests and storage migrations, then finish the bearer-token verifier and route-wide auth updates

### 2026-03-27 12:13 — Phase 6 tenant isolation red baseline
**Phase/Tasks:** 6C
**Status:** in-progress
**What:** added the first cross-tenant isolation tests in `tests/service/test_tenant_isolation.py`, covering foreign-tenant run reads, approval-resolution hiding, and denial audit emission against a tenant-scoped service auth config.
**Tests:** `uv run pytest -v tests/service/test_tenant_isolation.py` failed because tenant-scoped operators still hit the legacy default deployment scope during run creation
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6c-tenant-red-2026-03-27.txt`
**Blockers:** deployment, run/thread, approval, and audit persistence do not store tenant/workspace scope yet
**Next:** add storage migrations and scope propagation so deployment-scoped requests and persisted runtime objects carry tenant/workspace identity end to end

### 2026-03-27 12:18 — Phase 6 tenant scope persistence
**Phase/Tasks:** 6C
**Status:** in-progress
**What:** stamped deployment scope from graph deployment settings, added tenant/workspace persistence columns and migrations across deployments, runs/threads, approvals, and audits, and threaded tenant/workspace identity through runtime thread resolution and service-level scope checks.
**Tests:** `uv run pytest -v tests/service/test_tenant_isolation.py` passed
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6c-tenant-green-2026-03-27.txt`
**Blockers:** bearer/JWT verification and full service-suite auth contract updates are still pending
**Next:** add JWT bearer verification tests and dependency wiring, then update the existing service, approval, and audit suites to the authenticated Phase 6 API contract

### 2026-03-27 12:19 — Phase 6 bearer auth red baseline
**Phase/Tasks:** 6A
**Status:** in-progress
**What:** added `tests/service/test_bearer_auth.py` to verify RS256 bearer tokens against issuer, audience, and inline JWKS metadata, including rejection paths for invalid bearer credentials.
**Tests:** `uv run pytest -v tests/service/test_bearer_auth.py` failed during collection with `ModuleNotFoundError: No module named 'jwt'`
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6a-bearer-red-2026-03-27.txt`
**Blockers:** JWT and crypto dependencies are not installed yet
**Next:** add the JWT verification dependency, complete the bearer verifier path, and rerun the bearer auth suite

### 2026-03-27 12:21 — Phase 6 bearer verifier complete
**Phase/Tasks:** 6A
**Status:** completed
**What:** added `PyJWT[crypto]` to the runtime dependencies, completed the JWKS-backed bearer verifier path in `src/zeroth/service/auth.py`, and validated RS256 bearer auth for health requests including issuer, audience, and signature rejection paths.
**Tests:** `uv run pytest -v tests/service/test_bearer_auth.py` passed
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6a-bearer-green-2026-03-27.txt`
**Blockers:** full-suite contract updates for the older service, approval, and audit tests are still pending
**Next:** update the legacy service and repository tests to the authenticated Phase 6 contract, then run the focused verification batches and phase-wide lint

### 2026-03-27 12:35 — Phase 6 identity governance complete
**Phase/Tasks:** 6A, 6B, 6C, 6D
**Status:** completed
**What:** completed the authenticated-contract migration for the remaining service end-to-end and live research-audit scenario tests, updated the live research-audit bootstrap so it can accept Phase 6 auth wiring, finished the public run and approval identity lineage surfaces, and recorded the final verification evidence for the full Phase 6 service/live/approval/audit slice.
**Tests:** `uv run pytest -v tests/service tests/live_scenarios tests/approvals tests/audit` passed; `uv run ruff check src/ tests/` passed
**Artifacts:** `phases/phase-6-identity-governance/artifacts/test-6-service-live-suite-2026-03-27.txt`, `phases/phase-6-identity-governance/artifacts/lint-6-service-live-suite-2026-03-27.txt`
**Blockers:** none
**Next:** Phase 6 is closed; start Phase 7 when ready to expose audit timelines and provenance APIs

### 2026-03-27 12:48 — Phase 7 isolated worktree kickoff
**Phase/Tasks:** 7A
**Status:** in-progress
**What:** created an isolated worktree at `/Users/dondoe/coding/zeroth/.worktrees/codex-phase-7-transparent-governance`, synced the current repository state into it so Phase 6 uncommitted work remained available, and re-validated the focused baseline before starting the Phase 7 audit/timeline TDD slices.
**Tests:** `uv run pytest -q tests/audit/test_audit_repository.py tests/service/test_auth_api.py tests/service/test_rbac_api.py tests/service/test_contract_api.py` passed
**Artifacts:** none yet
**Blockers:** none
**Next:** add the first failing tests for public audit and timeline routes, then implement the minimum service surface to satisfy them

### 2026-03-27 12:49 — Phase 7 audit API red baseline
**Phase/Tasks:** 7A
**Status:** in-progress
**What:** added the first public audit/timeline API tests in `tests/service/test_audit_api.py` covering discoverability refs on run/deployment responses, deployment-scoped audit listing with response redaction, and run/deployment timeline routes.
**Tests:** `uv run pytest -q tests/service/test_audit_api.py` failed with missing `timeline_ref` in run responses and `404 Not Found` for the new audit/timeline routes
**Artifacts:** `phases/phase-7-transparent-governance/artifacts/test-7a-audit-api-red-2026-03-27.txt`
**Blockers:** none
**Next:** implement the audit API module, wire route registration, and extend run/deployment metadata responses with Phase 7 discoverability refs

### 2026-03-27 12:51 — Phase 7 audit and timeline surface complete
**Phase/Tasks:** 7A
**Status:** completed
**What:** added `src/zeroth/service/audit_api.py` with deployment-scoped audit query and timeline routes, wired route registration in `src/zeroth/service/app.py`, introduced response-layer redaction for sensitive audit payload fields, and extended public run/deployment metadata responses with Phase 7 discoverability refs for timelines and evidence bundles.
**Tests:** `uv run pytest -q tests/service/test_audit_api.py` passed
**Artifacts:** `phases/phase-7-transparent-governance/artifacts/test-7a-audit-api-green-2026-03-27.txt`
**Blockers:** none
**Next:** add failing tests for run and deployment evidence bundles, then implement the evidence builder layer and public export routes

### 2026-03-27 12:53 — Phase 7 evidence and attestation red baseline
**Phase/Tasks:** 7B, 7C, 7D
**Status:** in-progress
**What:** added failing tests for run/deployment evidence bundles, deployment attestation verification, and append-only audit continuity verification in `tests/service/test_evidence_api.py` and `tests/audit/test_audit_repository.py`.
**Tests:** `uv run pytest -q tests/audit/test_audit_repository.py tests/service/test_evidence_api.py` failed during collection because `AuditContinuityVerifier` does not exist yet
**Artifacts:** `phases/phase-7-transparent-governance/artifacts/test-7b-7d-red-2026-03-27.txt`
**Blockers:** none
**Next:** implement the audit digest/verifier layer, evidence builders and routes, then add deployment provenance digests and attestation verification

### 2026-03-27 12:58 — Phase 7 evidence, tamper detection, and attestation slice complete
**Phase/Tasks:** 7B, 7C, 7D
**Status:** completed
**What:** added append-only audit digest chaining and `AuditContinuityVerifier`, introduced review-friendly evidence bundle assembly for runs and deployments, extended approval querying for evidence exports, added deployment provenance digest fields plus attestation helpers, and wired public evidence/attestation routes into the service API.
**Tests:** `uv run pytest -q tests/audit/test_audit_repository.py tests/service/test_evidence_api.py tests/service/test_audit_api.py` passed
**Artifacts:** `phases/phase-7-transparent-governance/artifacts/test-7b-7d-green-2026-03-27.txt`
**Blockers:** none
**Next:** add the Phase 7 operator-facing documentation, then run the broader service/audit/deployment verification suite and lint

### 2026-03-27 12:59 — Phase 7 operator guide added
**Phase/Tasks:** 7B, 7D
**Status:** completed
**What:** added `docs/specs/phase-7-governance-evidence.md` documenting the public audit, timeline, evidence, and attestation endpoints plus the verification behavior for append-only audit chains and deployment snapshot digests.
**Tests:** not run
**Artifacts:** none
**Blockers:** none
**Next:** run the broader Phase 7 verification suite and lint, then close the phase gate if everything stays green

### 2026-03-27 13:02 — Phase 7 broader verification and gate close
**Phase/Tasks:** 7A, 7B, 7C, 7D
**Status:** completed
**What:** fixed the append-only audit regression by namespacing persisted runtime audit IDs with `run_id`, reran the failing phase-4, phase-5, and live research-audit continuity tests, and then completed the broader Phase 7 verification sweep across audit, service, deployments, approvals, and the live scenario.
**Tests:** `uv run pytest -q tests/service/test_e2e_phase4.py::test_phase4_end_to_end_deploy_invoke_resume_thread_and_rollback tests/service/test_e2e_phase5.py::test_phase5_thread_continuity_across_runs_via_api tests/live_scenarios/test_research_audit.py::test_research_audit_thread_continuity_across_runs` passed; `uv run pytest -q tests/audit tests/service tests/deployments tests/approvals tests/live_scenarios/test_research_audit.py` passed (`84 passed`); `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-7-transparent-governance/artifacts/test-7-phase-broader-2026-03-27.txt`, `phases/phase-7-transparent-governance/artifacts/lint-7-phase-broader-2026-03-27.txt`
**Blockers:** none
**Next:** Phase 7 is closed; start Phase 8 when ready to harden the runtime sandbox and policy-derived execution controls

### 2026-03-27 11:35 — Preserve and publish live research-audit scenario
**Phase/Tasks:** 5A
**Status:** in-progress
**What:** Preserved the local live research-audit scenario after remote history rewrite, prepared it for branch-based publication, and queued fresh verification before commit/push.
**Tests:** not run
**Artifacts:** none
**Blockers:** none
**Next:** Re-run targeted verification, commit the scenario on a feature branch, and push it to origin.

### 2026-03-27 11:38 — Verify live research-audit scenario for publication
**Phase/Tasks:** 5A
**Status:** in-progress
**What:** Re-ran the targeted live scenario test suite and lint checks on the preserved `live_scenarios/research_audit` implementation before staging it on a feature branch for push.
**Tests:** pass (`uv run pytest tests/live_scenarios/test_research_audit.py -v`, `uv run ruff check live_scenarios tests/live_scenarios`)
**Artifacts:** `phases/phase-5-integration/artifacts/test-live-scenario-push-2026-03-27.txt`, `phases/phase-5-integration/artifacts/lint-live-scenario-push-2026-03-27.txt`
**Blockers:** none
**Next:** Stage the live scenario deliverables, create the commit, and push the feature branch to origin.

### 2026-03-27 11:42 — Push live research-audit scenario branch
**Phase/Tasks:** 5A
**Status:** completed
**What:** Committed the preserved live research-audit scenario, documentation, tests, and verification artifacts on `codex/live-research-audit` and pushed the branch to `origin`.
**Tests:** pass (`uv run pytest tests/live_scenarios/test_research_audit.py -v`, `uv run ruff check live_scenarios tests/live_scenarios`)
**Artifacts:** `phases/phase-5-integration/artifacts/test-live-scenario-push-2026-03-27.txt`, `phases/phase-5-integration/artifacts/lint-live-scenario-push-2026-03-27.txt`
**Blockers:** none
**Next:** Open a PR or merge the feature branch when ready.

### 2026-03-27 11:48 — Verify feature branch before merge to main
**Phase/Tasks:** 5A
**Status:** in-progress
**What:** Re-ran the full repository test suite and lint checks on `codex/live-research-audit` immediately before merging it into `main`.
**Tests:** pass (`uv run pytest -v`, `uv run ruff check src tests live_scenarios`)
**Artifacts:** `phases/phase-5-integration/artifacts/test-full-before-merge-main-2026-03-27.txt`, `phases/phase-5-integration/artifacts/lint-before-merge-main-2026-03-27.txt`
**Blockers:** none
**Next:** Commit the fresh verification evidence, fast-forward `main`, verify the merged branch, and push `origin/main`.

### 2026-03-27 11:52 — Merge live research-audit into main
**Phase/Tasks:** 5A
**Status:** completed
**What:** Fast-forward merged `codex/live-research-audit` into `main`, re-verified the full repository on merged `main`, and prepared the final mainline push with post-merge evidence.
**Tests:** pass (`uv run pytest -v`, `uv run ruff check src tests live_scenarios`)
**Artifacts:** `phases/phase-5-integration/artifacts/test-full-after-merge-main-2026-03-27.txt`, `phases/phase-5-integration/artifacts/lint-after-merge-main-2026-03-27.txt`
**Blockers:** none
**Next:** Commit the post-merge verification evidence, push `origin/main`, and delete the local feature branch.

### 2026-03-27 11:54 — Publish live research-audit on main
**Phase/Tasks:** 5A
**Status:** completed
**What:** Pushed the merged live research-audit scenario to `origin/main` and deleted the local `codex/live-research-audit` branch after merge.
**Tests:** pass (`uv run pytest -v`, `uv run ruff check src tests live_scenarios`)
**Artifacts:** `phases/phase-5-integration/artifacts/test-full-after-merge-main-2026-03-27.txt`, `phases/phase-5-integration/artifacts/lint-after-merge-main-2026-03-27.txt`
**Blockers:** none
**Next:** Remote feature-branch cleanup is optional if you want it deleted as well.

<!-- Append iteration logs here. Format:
### YYYY-MM-DD HH:MM — [title]
**Phase/Tasks:** 1A, 2E, etc.
**Status:** completed | in-progress | blocked
**What:** [concrete changes, files created/modified]
**Tests:** [pass/fail/not run]
**Artifacts:** [files in phases/phase-N-*/artifacts/]
**Blockers:** [none or description]
**Next:** [what happens next]
-->

### 2026-03-19 13:12 — Phase 1A/1C Model Foundations Started
**Phase/Tasks:** 1A, 1C
**Status:** in-progress
**What:** added the core graph and edge-mapping domain modules plus storage/repository scaffolding.
- Added canonical graph/domain models in `src/zeroth/graph/models.py`
- Added graph serialization helpers in `src/zeroth/graph/serialization.py`
- Added graph SQLite schema/migration metadata in `src/zeroth/graph/storage.py`
- Added graph persistence repository in `src/zeroth/graph/repository.py`
- Added mapping schema, validator, and executor in `src/zeroth/mappings/models.py`, `src/zeroth/mappings/validator.py`, and `src/zeroth/mappings/executor.py`
> **Note:** graph definitions are being shaped to compile toward GovernAI `GovernedFlowSpec`, `GovernedStepSpec`, and `TransitionSpec`, with unresolved contracts/tools/policies/memory kept as plain refs/placeholders for later binding.
**Tests:** not run yet
**Artifacts:** none yet
**Blockers:** none
**Next:** run targeted graph/mapping tests, then capture passing test artifacts and mark completed checklist items

### 2026-03-19 13:20 — GovernAI-Compatible Graph Foundations Complete
**Phase/Tasks:** 1A, 1C
**Status:** completed
**What:** finished the GovernAI-aligned graph foundation and edge mapping layer.
- Added GovernAI flow/step/transition compilation helpers to `src/zeroth/graph/models.py`
- Added graph persistence round-trip coverage in `tests/graph/test_repository.py`
- Added GovernAI integration assertions in `tests/graph/test_models.py`
- Added mapping executor and validator coverage in `tests/mappings/test_executor.py` and `tests/mappings/test_validator.py`
**Tests:** `uv run pytest -v tests/graph tests/mappings tests/test_smoke.py` passed; `uv run ruff check src/zeroth/graph src/zeroth/mappings tests/graph tests/mappings` passed
**Artifacts:** `phases/phase-1-foundation/artifacts/test-phase1-graph-mappings-governai-clean-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-graph-mappings-governai-clean-2026-03-19.txt`
**Blockers:** none
**Next:** Phase 1D or the remaining phase 1 streams can build on the GovernAI-compatible graph and mapping model

### 2026-03-19 12:58 — Phase 1 Shared Storage Base
**Phase/Tasks:** 1A, 1B, 1C, 1D
**Status:** in-progress
**What:** established the shared Phase 1 storage and test foundation for parallel streams.
- Added SQLite migration/version helpers in `src/zeroth/storage/sqlite.py`
- Added JSON serialization helpers for Pydantic-backed repositories in `src/zeroth/storage/json.py`
- Added shared storage exports in `src/zeroth/storage/__init__.py` and package exports in `src/zeroth/__init__.py`
- Added pytest SQLite fixture in `tests/conftest.py`
- Added storage helper coverage in `tests/storage/test_json.py` and `tests/storage/test_sqlite.py`
**Tests:** `uv run pytest -v tests/storage tests/test_smoke.py` passed; initial `uv run ruff check src tests` failed on import cleanup, rerun passed after fix
**Artifacts:** `phases/phase-1-foundation/artifacts/test-phase1-shared-storage-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-shared-storage-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-shared-storage-rerun-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-shared-storage-clean-2026-03-19.txt`
**Blockers:** none
**Next:** spawn parallel workers for the contract registry, run/thread persistence, and graph/mapping models on top of the shared storage base

### 2026-03-19 13:06 — GovernAI-Aligned Run/Thread Persistence
**Phase/Tasks:** 1D
**Status:** completed
**What:** revised the run layer to build on local GovernAI run semantics rather than a parallel core model.
- Made `Run` a subclass of GovernAI `RunState` in `src/zeroth/runs/models.py`
- Reused GovernAI `RunStatus` values and aligned run transitions to `PENDING`, `RUNNING`, `WAITING_APPROVAL`, `WAITING_INTERRUPT`, `COMPLETED`, and `FAILED`
- Added SQLite-backed run checkpointing and thread-aware run indexing in `src/zeroth/runs/repository.py`
- Added/updated tests in `tests/runs/test_models.py` and `tests/runs/test_repository.py` to cover GovernAI defaults, checkpoint semantics, thread continuation, and active-run tracking
**Tests:** `PYTHONPATH=src .venv/bin/python -m pytest -v tests/runs tests/test_smoke.py` passed; `.venv/bin/ruff check src/zeroth/runs tests/runs tests/test_smoke.py` passed
**Artifacts:** `phases/phase-1-foundation/artifacts/test-phase1-runs-governai-align-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-runs-governai-align-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/test-phase1-runs-governai-align-rerun-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-runs-governai-align-rerun-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-runs-governai-align-clean-2026-03-19.txt`
**Blockers:** none
**Next:** move to the next Phase 1 stream, with the run/thread foundation now aligned to GovernAI

### 2026-03-19 13:03 — Phase 1B Contract Registry
**Phase/Tasks:** 1B
**Status:** completed
**What:** implemented a SQLite-backed versioned contract registry for typed Pydantic models and added GovernAI adapters for tool and step metadata.
- Added versioned contract records, reference resolution, and runtime type reconstruction in `src/zeroth/contracts/registry.py`
- Added error types in `src/zeroth/contracts/errors.py`
- Added public exports in `src/zeroth/contracts/__init__.py`
- Added CRUD, schema-shape, missing-version, and GovernAI integration tests in `tests/contracts/test_registry.py`
**Test results:** `uv run pytest -v tests/contracts` passed; `uv run ruff check src/zeroth/contracts tests/contracts` passed
**Artifacts produced:** `phases/phase-1-foundation/artifacts/test-phase1-contract-registry-governai-final-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-contract-registry-governai-final-2026-03-19.txt`
**Blockers:** none
**Next:** continue with Phase 1A/1C/1D completion and then Phase 1E/1F

### 2026-03-19 13:07 — Phase 1A-1D Mainline Integration
**Phase/Tasks:** 1A, 1B, 1C, 1D
**Status:** completed
**What:** integrated the parallel Phase 1 streams into a single GovernAI-based foundation batch and resolved main-workspace integration issues.
- Added `tests/contracts/__init__.py`, `tests/graph/__init__.py`, `tests/mappings/__init__.py`, `tests/runs/__init__.py`, and `tests/storage/__init__.py` so pytest collects subpackages without module-name collisions
- Re-validated the combined graph, mapping, contract, run/thread, and storage slices together in the main workspace
- Confirmed Zeroth now imports the sibling GovernAI package through the project dependency configuration in `pyproject.toml`
**Tests:** `uv run pytest -v tests/contracts tests/runs tests/graph tests/mappings tests/storage tests/test_smoke.py` passed; `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-1-foundation/artifacts/test-phase1-foundation-batch-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-foundation-batch-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/test-phase1-foundation-batch-clean-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-foundation-batch-clean-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/governai-integration-2026-03-19.txt`
**Blockers:** none
**Next:** implement Phase 1E graph validation and Phase 1F graph CRUD/versioning on top of the now-green GovernAI-aligned models, registry, mappings, and run/thread persistence

### 2026-03-19 13:14 — Phase 1E-1F Graph Validation And Versioning
**Phase/Tasks:** 1E, 1F
**Status:** completed
**What:** completed the dependent graph-validation and graph-versioning slices on top of the GovernAI-aligned foundation and revalidated the full Phase 1 mainline.
- Added structured graph validation report and issue taxonomy in `src/zeroth/graph/validation.py` and `src/zeroth/graph/validation_errors.py`
- Added graph version cloning and semantic diff support in `src/zeroth/graph/versioning.py` and `src/zeroth/graph/diff.py`
- Extended `src/zeroth/graph/repository.py` with immutable published versions, clone-to-draft flow, version history queries, and diff lookup
- Added validation coverage in `tests/graph/test_validation.py`
- Added lifecycle, immutability, clone/history, and diff coverage in `tests/graph/test_repository.py`
- Ran the full Phase 1 test and lint batch after integration to confirm 1A-1F works together in the main workspace
**Tests:** `uv run pytest -v tests/graph tests/mappings tests/test_smoke.py` passed; `uv run ruff check src/zeroth/graph tests/graph tests/mappings` passed; `uv run pytest -v tests/contracts tests/runs tests/graph tests/mappings tests/storage tests/test_smoke.py` passed; `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-1-foundation/artifacts/test-phase1-graph-validation-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-graph-validation-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-graph-validation-clean-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/test-phase1-graph-crud-versioning-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-graph-crud-versioning-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-graph-crud-versioning-clean-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/test-phase1-foundation-1a-1f-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-phase1-foundation-1a-1f-2026-03-19.txt`
**Blockers:** none
**Next:** start Phase 2 with executable-unit manifests/runtime adapters, agent runtime, and condition/branch resolution as the next parallel streams

### 2026-03-19 13:20 — Phase 2 Entry Streams
**Phase/Tasks:** 2A, 2C, 2E, 2G
**Status:** completed
**What:** completed the first independent Phase 2 execution streams and validated them together in the main workspace.
- Added executable-unit manifests, validation, and GovernAI-backed runtime adapters in `src/zeroth/execution_units/`
- Added typed agent runtime config, prompt assembly, provider adapters, output validation, and retry/timeout handling in `src/zeroth/agent_runtime/`
- Added deterministic condition evaluation, branch planning, and run-state condition recording in `src/zeroth/conditions/`
- Added focused tests in `tests/execution_units/`, `tests/agent_runtime/`, and `tests/conditions/`
- Ran the combined Phase 2 entry-stream batch to confirm the three packages work together, not just in isolation
**Tests:** `uv run pytest -v tests/execution_units tests/agent_runtime tests/conditions tests/test_smoke.py` passed; `uv run ruff check src/zeroth/execution_units src/zeroth/agent_runtime src/zeroth/conditions tests/execution_units tests/agent_runtime tests/conditions` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-phase2-execution-units-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-execution-units-clean-2026-03-19.txt`, `phases/phase-2-execution/artifacts/test-phase2-agent-runtime-clean-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-agent-runtime-clean-2026-03-19.txt`, `phases/phase-2-execution/artifacts/test-phase2-conditions-clean-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-conditions-clean-2026-03-19.txt`, `phases/phase-2-execution/artifacts/test-phase2-entry-streams-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-entry-streams-2026-03-19.txt`
**Blockers:** none
**Next:** implement input/output mode handling (2B), sandbox execution and environment caching (2D), tool attachment (2F), thread checkpoint/restore integration (2I), then converge the streams in the runtime orchestrator (2H)

### 2026-03-19 13:32 — Phase 2 Parallel Implementation Kickoff
**Phase/Tasks:** 2B, 2D, 2F, 2I
**Status:** in-progress
**What:** started the remaining independent Phase 2 streams as parallel implementation tracks and locked the plan to the current repo state.
- Marked 2B, 2D, 2F, and 2I as active workstreams in the root progress tracker
- Confirmed current execution foundations in `src/zeroth/execution_units/`, `src/zeroth/agent_runtime/`, `src/zeroth/conditions/`, and `src/zeroth/runs/`
- Confirmed the implementation should continue using the local `governai` checkout referenced by `pyproject.toml`
- Prepared to split work across subagents with disjoint ownership to reduce context pressure before orchestrator convergence in 2H
**Tests:** not yet run
**Artifacts:** none
**Blockers:** none
**Next:** spawn dedicated workers for 2B, 2D, 2F, and 2I, then integrate their changes and implement 2H on top

### 2026-03-19 13:48 — Phase 2I Thread Store Foundations
**Phase/Tasks:** 2I
**Status:** completed
**What:** added a repository-backed thread resolver and state store in `src/zeroth/agent_runtime/thread_store.py`.
- Thread resolution now delegates to the existing `ThreadRepository.resolve` flow and exposes explicit `resolve_optional` no-op behavior for stateless invocations
- Thread-scoped checkpoints now persist directly to the shared `run_checkpoints` table with a dedicated `thread_state` metadata marker, keeping restore data separate from audit payloads and logical run history
- Added focused coverage in `tests/agent_runtime/test_thread_store.py` for create/continue, checkpoint restore, and stateless no-op behavior
**Tests:** `uv run pytest -q tests/agent_runtime/test_thread_store.py` passed; `uv run pytest -q tests/runs/test_repository.py tests/agent_runtime/test_thread_store.py` passed; `uv run ruff check src/zeroth/agent_runtime/thread_store.py tests/agent_runtime/test_thread_store.py` passed
**Artifacts:** none
**Blockers:** none
**Next:** hand off the thread store to the orchestrator and agent-runtime integration workstreams

### 2026-03-19 13:41 — Phase 2B Input/Output Helpers
**Phase/Tasks:** 2B
**Status:** completed
**What:** added self-contained input injection, output extraction, and typed-output conversion helpers for executable units.
- Added `src/zeroth/execution_units/io.py` with helpers for `json_stdin`, `cli_args`, `env_vars`, `input_file_json`, `json_stdout`, `tagged_stdout_json`, `output_file_json`, `text_stdout`, and `exit_code_only`
- Added `tests/execution_units/test_io.py` covering happy paths and failure paths for each supported mode plus typed-model validation
- Captured passing pytest and ruff output in phase artifacts for the 2B stream
**Tests:** `uv run pytest -q tests/execution_units/test_io.py` passed; `uv run ruff check src/zeroth/execution_units/io.py tests/execution_units/test_io.py` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-phase2-io-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-io-2026-03-19.txt`
**Blockers:** none
**Next:** extend the remaining Phase 2 streams for sandbox execution, tool attachment, and thread checkpoint/restore

### 2026-03-19 13:48 — Phase 2F Tool Attachment Foundations
**Phase/Tasks:** 2F
**Status:** completed
**What:** added a self-contained tool attachment registry/bridge layer and focused tests without wiring it into `AgentRunner` yet.
- Added `src/zeroth/agent_runtime/tools.py` with typed attachment manifests, alias resolution, declared-tool enforcement, permission checks, and audit helpers
- Added `tests/agent_runtime/test_tools.py` covering alias resolution, undeclared-tool rejection, permission enforcement, and audit payloads
- Kept the module integration-ready for later agent-runtime wiring while preserving the strict file boundary for this task
**Tests:** `uv run pytest -q tests/agent_runtime/test_tools.py` passed; `uv run ruff check src/zeroth/agent_runtime/tools.py tests/agent_runtime/test_tools.py` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-phase2-tool-attachment-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-tool-attachment-2026-03-19.txt`
**Blockers:** none
**Next:** integrate this attachment layer into the remaining Phase 2 streams when the orchestrator and sandbox layers are ready

### 2026-03-19 14:02 — Phase 2 Convergence Started
**Phase/Tasks:** 2H
**Status:** in-progress
**What:** started the shared-file integration pass now that the independent 2B, 2D, 2F, and 2I foundations are landing.
- Verified the returned worker modules in `src/zeroth/execution_units/io.py`, `src/zeroth/execution_units/sandbox.py`, `src/zeroth/agent_runtime/tools.py`, and `src/zeroth/agent_runtime/thread_store.py`
- Confirmed local GovernAI alignment points: CLI tools remain JSON-stdin/JSON-stdout only, normalized tool calls already exist, and thread semantics remain caller-supplied `thread_id` with active/latest run lookup
- Reserved the remaining local work for shared-file wiring, executable-unit runner integration, and the Phase 2 runtime orchestrator
**Tests:** not yet run
**Artifacts:** none
**Blockers:** none
**Next:** wire the new execution-unit, tool-attachment, and thread-state foundations into the shared runtime surfaces and implement the orchestrator end-to-end

### 2026-03-19 14:10 — Phase 2 Shared Runtime Wiring
**Phase/Tasks:** 2D, 2F, 2I, 2H
**Status:** in-progress
**What:** added the shared runtime layer that connects the independent Phase 2 foundations without touching the orchestrator package yet.
- Added `src/zeroth/execution_units/runner.py` plus `tests/execution_units/test_runner.py` for registry-backed executable-unit execution over Zeroth I/O helpers and the local sandbox foundations
- Extended agent runtime exports and integrated tool-call/thread-state behavior so `AgentRunner` can execute GovernAI-style normalized tool calls against declared executable units and persist repository-backed thread state
- Added `tests/agent_runtime/test_runner_integration.py` covering tool-call execution through executable units and repository-backed thread checkpoint integration
**Tests:** `uv run pytest -q tests/execution_units/test_runner.py tests/agent_runtime/test_runner_integration.py` passed; `uv run ruff check src/zeroth/execution_units/runner.py src/zeroth/agent_runtime tests/execution_units/test_runner.py tests/agent_runtime/test_runner_integration.py` failed
**Artifacts:** none
**Blockers:** minor lint issues in `src/zeroth/agent_runtime/__init__.py` and `tests/agent_runtime/test_runner_integration.py`
**Next:** fix lint, rerun the focused runtime suites, then merge in the dedicated orchestrator implementation and run the integrated Phase 2 checks

### 2026-03-19 14:12 — Phase 2 Shared Runtime Wiring Clean
**Phase/Tasks:** 2D, 2F, 2I, 2H
**Status:** in-progress
**What:** cleaned up the shared runtime layer so the new executable-unit runner and agent-runtime integration surfaces are green before orchestrator convergence.
- Fixed the remaining lint issues in `src/zeroth/agent_runtime/__init__.py` and `tests/agent_runtime/test_runner_integration.py`
- Revalidated the new executable-unit runner and agent-runtime integration coverage after the cleanup pass
**Tests:** `uv run pytest -q tests/execution_units/test_runner.py tests/agent_runtime/test_runner_integration.py` passed; `uv run ruff check src/zeroth/execution_units/runner.py src/zeroth/agent_runtime tests/execution_units/test_runner.py tests/agent_runtime/test_runner_integration.py` passed
**Artifacts:** none
**Blockers:** none
**Next:** merge in the dedicated 2H orchestrator implementation, resolve any interface gaps, and run the integrated Phase 2 execution suites

### 2026-03-19 14:11 — Shared Runtime Wiring Pass 1
**Phase/Tasks:** 2D, 2F, 2I, 2H
**Status:** in-progress
**What:** added the first shared runtime wiring layer on top of the landed worker modules.
- Added `src/zeroth/execution_units/runner.py` with a typed executable-unit registry and runner that bridges manifests, I/O helpers, sandbox execution, and native Python handlers
- Extended the agent runtime models, provider adapter, runner, and exports to support repository-backed thread stores plus GovernAI-style normalized tool calls
- Added focused tests in `tests/execution_units/test_runner.py` and `tests/agent_runtime/test_runner_tools.py` for executable-unit execution and agent tool-call loops
**Tests:** `uv run pytest -q tests/execution_units/test_runner.py tests/agent_runtime/test_runner_tools.py` passed; initial `uv run ruff check ...` failed on import ordering and line-length issues, and follow-up fixes are now applied
**Artifacts:** none
**Blockers:** none
**Next:** rerun lint on the shared runtime surfaces, then implement the orchestrator package and integrated execution-path tests

### 2026-03-19 14:18 — Shared Runtime Wiring Pass 1 Green
**Phase/Tasks:** 2D, 2F, 2I, 2H
**Status:** completed
**What:** finished the shared runtime wiring needed before orchestrator implementation.
- Finalized `src/zeroth/execution_units/runner.py` so executable units can be resolved and executed through Zeroth-owned I/O plus the sandbox layer
- Finalized agent-runtime tool-call and thread-store integration across `src/zeroth/agent_runtime/models.py`, `prompt.py`, `provider.py`, `runner.py`, and package exports
- Added and validated focused execution-path tests in `tests/execution_units/test_runner.py` and `tests/agent_runtime/test_runner_tools.py`
**Tests:** `uv run pytest -q tests/execution_units/test_runner.py tests/agent_runtime/test_runner_tools.py` passed; `uv run ruff check src/zeroth/execution_units/runner.py src/zeroth/agent_runtime/models.py src/zeroth/agent_runtime/prompt.py src/zeroth/agent_runtime/provider.py src/zeroth/agent_runtime/runner.py src/zeroth/agent_runtime/tools.py tests/execution_units/test_runner.py tests/agent_runtime/test_runner_tools.py` passed
**Artifacts:** none
**Blockers:** none
**Next:** implement `src/zeroth/orchestrator/` and integrated graph-execution tests for the remaining 2H gate

### 2026-03-19 14:26 — Phase 2 Orchestrator Gate Complete
**Phase/Tasks:** 2H
**Status:** completed
**What:** implemented and validated the runtime orchestrator on top of the new execution-unit, tool-attachment, and thread-state foundations.
- Added `src/zeroth/orchestrator/runtime.py` and `src/zeroth/orchestrator/__init__.py` with async graph execution, node dispatch, lifecycle transitions, branch planning, loop guards, approval pause stubs, and persisted checkpoint updates
- Added `tests/orchestrator/test_runtime.py` covering linear execution, conditional branching, cycle termination, approval pause, and persisted run resume
- Finalized shared execution-path wiring in `src/zeroth/execution_units/runner.py` plus agent runtime tool-call and thread-store integration so the orchestrator can execute end-to-end against the local GovernAI-backed runtime surfaces
- Revalidated the full Phase 2 stream suite in one batch and captured gate artifacts
**Tests:** `uv run pytest -v tests/execution_units tests/agent_runtime tests/conditions tests/orchestrator tests/runs/test_repository.py` passed; `uv run ruff check src/zeroth/execution_units src/zeroth/agent_runtime src/zeroth/conditions src/zeroth/orchestrator tests/execution_units tests/agent_runtime tests/conditions tests/orchestrator` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-phase2-orchestrator-gate-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-phase2-orchestrator-gate-2026-03-19.txt`
**Blockers:** none
**Next:** Phase 2 is complete; Phase 3 can start with memory connectors, approval handling, and audit-system expansion on top of the now-green execution core

### 2026-03-19 14:29 — Repository-Wide Validation After Phase 2
**Phase/Tasks:** 2H
**Status:** completed
**What:** ran the full repository validation pass after closing the Phase 2 gate to check for regressions outside the focused execution-core suites.
- Verified the entire test suite still passes after the new execution-unit runner, agent runtime integrations, and orchestrator changes
- Verified repo-wide lint remains clean across `src/` and `tests/`
**Tests:** `uv run pytest -q` passed; `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-repo-validation-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-repo-validation-2026-03-19.txt`
**Blockers:** none
**Next:** execution core is stable; the next implementation stream is Phase 3 platform control

### 2026-03-19 14:25 — Phase 2D Sandbox Foundations
**Phase/Tasks:** 2D
**Status:** completed
**What:** implemented the local sandbox foundations requested for Phase 2D and verified them with focused tests.
- Added `src/zeroth/execution_units/sandbox.py` with a tempdir-based subprocess manager, env allowlisting/overlay, timeout handling, workdir cleanup, and deterministic environment-cache helpers
- Added `tests/execution_units/test_sandbox.py` covering isolation basics, timeout handling, cache hit/miss behavior, and cache-key uniqueness
- Captured the test run in `phases/phase-2-execution/artifacts/test-phase2-sandbox-2026-03-19.txt`
**Tests:** `uv run pytest -q tests/execution_units/test_sandbox.py` passed; `uv run ruff check src/zeroth/execution_units/sandbox.py tests/execution_units/test_sandbox.py` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-phase2-sandbox-2026-03-19.txt`
**Blockers:** none
**Next:** continue with the remaining Phase 2 streams and then converge on the orchestrator in 2H

### 2026-03-19 15:26 — Redis Backend Viability Investigation
**Phase/Tasks:** 1B, 1D, 1F, 2H, 2I
**Status:** completed
**What:** investigated whether GovernAI's Redis support is enough to replace Zeroth's current main persistence layer.
- Verified that GovernAI currently ships Redis-backed runtime primitives for run state, checkpoints, thread run indexes, interrupts, and audit events
- Verified that Zeroth still owns separate persistence concerns for contracts, graphs/version history, and richer thread documents that are currently implemented on SQLite repositories
- Wrote the migration/gap analysis to `phases/phase-2-execution/artifacts/redis-backend-investigation-2026-03-19.md`
**Tests:** not run; investigation only
**Artifacts:** `phases/phase-2-execution/artifacts/redis-backend-investigation-2026-03-19.md`
**Blockers:** GovernAI Redis is not sufficient by itself for Zeroth's full persistence surface; additional Zeroth Redis repositories would be required for a full backend switch
**Next:** decide whether to do a runtime-state-only Redis migration or implement full Zeroth Redis repositories for contracts, graphs, and thread documents

### 2026-03-19 15:34 — Redis Configuration Foundations
**Phase/Tasks:** 1D, 2H, 2I
**Status:** completed
**What:** added the first option-2 implementation slice: a configurable Redis settings layer plus GovernAI runtime-store factories.
- Added `src/zeroth/storage/redis.py` with typed Redis configuration covering local, Docker, and remote deployments, URL masking, env loading, and Docker bootstrap command generation
- Added GovernAI factory wiring so one resolved Redis config can materialize `RedisRunStore`, `RedisInterruptStore`, and `RedisAuditEmitter` with consistent Zeroth key prefixes
- Exported the new storage types through `src/zeroth/storage/__init__.py` and `src/zeroth/__init__.py`
- Added `redis>=5.0.0` to `pyproject.toml` for the Redis-backed path
- Added focused coverage in `tests/storage/test_redis.py`
**Tests:** `uv run pytest -q tests/storage/test_redis.py tests/storage/test_sqlite.py` passed; `uv run ruff check src/zeroth/storage src/zeroth/__init__.py tests/storage/test_redis.py tests/storage/test_sqlite.py` passed
**Artifacts:** `phases/phase-1-foundation/artifacts/test-redis-config-2026-03-19.txt`, `phases/phase-1-foundation/artifacts/lint-redis-config-2026-03-19.txt`
**Blockers:** none
**Next:** introduce Redis-backed Zeroth repositories behind the new config layer, starting with runs/threads so the current runtime can be switched off SQLite incrementally

### 2026-03-19 16:02 — Provisioned Docker Backend Controls
**Phase/Tasks:** 2D
**Status:** completed
**What:** updated Redis and sandbox backend behavior so Docker is treated as a pre-provisioned dependency instead of something Zeroth creates at runtime.
- Reworked `src/zeroth/storage/redis.py` so Docker mode now targets a configured running container/service host and exposes container-availability checks instead of `docker run` generation
- Extended `src/zeroth/execution_units/sandbox.py` with backend config for `local`, `docker`, and `auto`, plus Docker-container availability checks and provisioned-container command execution via `docker exec`
- Kept local subprocess execution as the default path, while allowing callers to select a dedicated Docker sandbox container when one is already provisioned
- Exported the new sandbox and Redis backend types through package `__init__` files and expanded focused tests in `tests/storage/test_redis.py` and `tests/execution_units/test_sandbox.py`
**Tests:** `uv run pytest -q tests/storage/test_redis.py tests/execution_units/test_sandbox.py tests/execution_units/test_runner.py` passed; `uv run ruff check src/zeroth/storage src/zeroth/execution_units src/zeroth/__init__.py tests/storage/test_redis.py tests/execution_units/test_sandbox.py tests/execution_units/test_runner.py` passed
**Artifacts:** `phases/phase-2-execution/artifacts/test-docker-aware-sandbox-2026-03-19.txt`, `phases/phase-2-execution/artifacts/lint-docker-aware-sandbox-2026-03-19.txt`
**Blockers:** none
**Next:** wire Redis-backed Zeroth repositories and add higher-level app/runtime config so deployment can switch both persistence and sandbox backend from environment without code changes

### 2026-03-20 00:00 — Service Wrapper Red Baseline
**Phase/Tasks:** 4B
**Status:** in-progress
**What:** ran `uv run pytest -q tests/service/test_app.py` before implementation; collection failed because `fastapi` is missing and the `zeroth.service` package has not been created yet.
**Tests:** failed during collection with `ModuleNotFoundError: No module named 'fastapi'`
**Artifacts:** none yet
**Blockers:** service dependencies and package skeleton are missing
**Next:** add `fastapi`, `httpx`, and `uvicorn`, then implement `src/zeroth/service/*`

### 2026-03-20 00:01 — Service Wrapper Lint Check
**Phase/Tasks:** 4B
**Status:** in-progress
**What:** ran `uv run ruff check src/zeroth/service tests/service/test_app.py` after the first implementation pass; lint reported one fixable import-order issue in `src/zeroth/service/bootstrap.py`.
**Tests:** failed lint check (`I001` import block unsorted)
**Artifacts:** none yet
**Blockers:** bootstrap import block needs reformatting
**Next:** sort the imports, rerun lint, then capture passing test output as an artifact

### 2026-03-20 00:02 — Service Wrapper Skeleton Complete
**Phase/Tasks:** 4B
**Status:** completed
**What:** added the deployment-bound service wrapper package with a FastAPI app factory, a bootstrap container that wires deployment service, run repository, approval service, audit repository, contract registry, and orchestrator, plus a minimal `/health` endpoint.
**Tests:** `uv run pytest -q tests/service/test_app.py` passed; `uv run ruff check src/zeroth/service tests/service/test_app.py` passed
**Artifacts:** `phases/phase-4-deployment/artifacts/test-4b-service-wrapper-2026-03-20.txt`
**Blockers:** none
**Next:** start 4C only when ready to add run creation and status endpoints

### 2026-03-20 00:03 — Service Bootstrap Entry Point Gap
**Phase/Tasks:** 4B
**Status:** in-progress
**What:** added a red test path for `bootstrap_app(sqlite_db, deployment_ref=...)`; collection now fails because `bootstrap_app` is not exported from `src/zeroth/service/bootstrap.py`.
**Tests:** failed during collection with `ImportError: cannot import name 'bootstrap_app'`
**Artifacts:** none yet
**Blockers:** bootstrap-to-app entrypoint is missing
**Next:** add the deployment-bound app bootstrap helper and rerun the service tests

### 2026-03-20 00:04 — Service Bootstrap Entry Point Complete
**Phase/Tasks:** 4B
**Status:** completed
**What:** added `bootstrap_app(database, deployment_ref)` in `src/zeroth/service/bootstrap.py` so the service can be bootstrapped directly from a deployment snapshot into a FastAPI app; updated exports and explicit coverage in `tests/service/test_app.py`.
**Tests:** `uv run pytest -q tests/service/test_app.py` passed; `uv run ruff check src/zeroth/service tests/service/test_app.py` passed
**Artifacts:** `phases/phase-4-deployment/artifacts/test-4b-service-wrapper-2026-03-20.txt`
**Blockers:** none
**Next:** continue with 4C only when run creation/status endpoints are in scope

### 2026-03-20 00:05 — Phase 4 E2E Verification Added
**Phase/Tasks:** 4B, 4C, 4D, 4E
**Status:** completed
**What:** added the final phase-4 end-to-end service test in `tests/service/test_e2e_phase4.py`, covering publish, deploy, run invocation, polling, approval resolution, contract metadata, thread continuity, and rollback using the current service routes and deployment service snapshot flow.
**Tests:** `uv run pytest -q tests/service/test_e2e_phase4.py` passed
**Artifacts:** none
**Blockers:** none
**Next:** run the broader service test set if you want extra confidence beyond the targeted e2e verification

### 2026-03-26 15:58 — Phase 5 Integration And Specs Complete
**Phase/Tasks:** 5A, 5B
**Status:** completed
**What:** added the Phase 5 API-level end-to-end suite in `tests/service/test_e2e_phase5.py`, extracted shared service-test helpers into `tests/service/helpers.py`, and wrote the five implementation-facing spec documents under `docs/specs/`.
**Tests:** `uv run pytest -v tests/service/test_e2e_phase5.py` passed; `uv run pytest -v tests/service tests/orchestrator tests/approvals tests/memory tests/policy` passed; `uv run pytest -v` passed after the final lint cleanup; `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-5-integration/artifacts/test-phase5-e2e-full-2026-03-26.txt`, `phases/phase-5-integration/artifacts/test-phase5-focused-suite-2026-03-26.txt`, `phases/phase-5-integration/artifacts/test-phase5-post-lint-service-2026-03-26.txt`, `phases/phase-5-integration/artifacts/test-phase5-repo-full-rerun-2026-03-26.txt`, `phases/phase-5-integration/artifacts/lint-phase5-full-rerun-2026-03-26.txt`, `phases/phase-5-integration/artifacts/review-phase5-spec-docs-2026-03-26.txt`
**Blockers:** none
**Next:** Phase 5 is closed and the MVP is marked shippable

### 2026-03-27 20:04 — Phase 8 Baseline And Worktree Setup
**Phase/Tasks:** 8A
**Status:** in-progress
**What:** created the isolated Phase 8 worktree on `codex/phase-8-runtime-security`, synced the Python environment with `uv sync`, and captured a pre-change baseline for the relevant sandbox, policy, and orchestrator tests before starting TDD for runtime hardening.
**Tests:** `uv run pytest -q tests/execution_units/test_sandbox.py tests/policy/test_guard.py tests/orchestrator/test_runtime.py` failed with one pre-existing failure in `tests/orchestrator/test_runtime.py::test_runtime_orchestrator_continues_after_approval_resolution` because `ApprovalService.resolve()` no longer accepts the `approver=` keyword used by the test
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-phase8-baseline-2026-03-27.txt`
**Blockers:** baseline is not fully green due to the unrelated approval-resolution test mismatch
**Next:** add failing 8A sandbox hardening tests, implement the new strictness/resource-constraint behavior, and keep the unrelated approval test failure separate from Phase 8 regressions

### 2026-03-27 20:06 — 8A Sandbox Hardening Red Test
**Phase/Tasks:** 8A
**Status:** in-progress
**What:** added `tests/execution_units/test_sandbox_hardening.py` to pin the new sandbox hardening API and behavior before implementation, covering strictness modes, Docker resource-flag translation, Docker constraint propagation, and isolation-policy failures.
**Tests:** `uv run pytest -q tests/execution_units/test_sandbox_hardening.py` failed during collection with `ModuleNotFoundError: No module named 'zeroth.execution_units.constraints'`
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-8a-sandbox-hardening-red-2026-03-27.txt`
**Blockers:** the new constraints module and sandbox hardening types are not implemented yet
**Next:** add the constraints module, extend sandbox config/manager strictness handling, and rerun the new 8A test suite to drive the implementation green

### 2026-03-27 20:08 — 8A Sandbox Hardening Layer Implemented
**Phase/Tasks:** 8A
**Status:** in-progress
**What:** added `src/zeroth/execution_units/constraints.py`, extended `src/zeroth/execution_units/sandbox.py` with strictness-aware backend resolution and policy-violation errors, switched Docker execution to an ephemeral `docker run` path that can carry per-run resource flags, and updated the execution-unit exports plus legacy sandbox regression coverage.
**Tests:** `uv run pytest -q tests/execution_units/test_sandbox_hardening.py tests/execution_units/test_sandbox.py` passed
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-8a-sandbox-hardening-green-2026-03-27.txt`
**Blockers:** none in the sandbox layer; runtime enforcement wiring is still pending
**Next:** start 8B by writing failing runtime-enforcement tests around timeout, secret filtering, strictness propagation, approval gating, and audit metadata

### 2026-03-27 20:16 — 8B Runtime Enforcement Wired
**Phase/Tasks:** 8B
**Status:** in-progress
**What:** extended policy enforcement results with side-effect approval flags, stored node-scoped enforcement context on runs, passed that context into agent and executable-unit runners, applied policy-derived timeout/secret/network/strictness overrides in the runners, recorded enforcement metadata in audits, and added side-effect approval gating with resume support for policy-paused nodes.
**Tests:** `uv run pytest -q tests/policy/test_runtime_enforcement.py tests/policy/test_guard.py tests/execution_units/test_runner.py tests/agent_runtime/test_runner_tools.py tests/agent_runtime/test_runner_integration.py tests/agent_runtime/test_agent_runtime.py` passed; `uv run pytest -q tests/policy/test_guard.py tests/orchestrator/test_runtime.py` still reports the same pre-existing `ApprovalService.resolve(... approver=...)` failure from the baseline
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-8b-runtime-enforcement-green-2026-03-27.txt`
**Blockers:** broader retry/background-preservation coverage is still open, and the unrelated approval-service test mismatch remains in the baseline suite
**Next:** implement 8C secret providers, runtime secret resolution/redaction, and local at-rest protection without regressing the new enforcement path

### 2026-03-27 20:21 — 8C Secret Resolution And Data Protection Added
**Phase/Tasks:** 8C
**Status:** in-progress
**What:** added the `zeroth.secrets` package with environment-backed secret resolution and value-based redaction, taught executable-unit execution to resolve `EnvironmentVariable.secret_ref` entries, redacted resolved secrets before audit persistence, and added optional Fernet-backed encryption for audit-record and checkpoint JSON columns in SQLite-backed repositories.
**Tests:** `uv run pytest -q tests/secrets/test_provider.py tests/secrets/test_data_protection.py tests/audit/test_audit_repository.py tests/agent_runtime/test_thread_store.py tests/runs/test_repository.py tests/storage/test_sqlite.py` passed
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-8c-secret-protection-green-2026-03-27.txt`
**Blockers:** approval-record secret handling is not yet covered explicitly, so the "approvals do not retain secret material" check remains open
**Next:** implement 8D manifest digesting and admission control, then finish the remaining Phase 8 verification and scope gaps

### 2026-03-27 20:30 — 8D Integrity And Phase 8 Verification Complete
**Phase/Tasks:** 8D, Phase 8 Gate
**Status:** completed
**What:** added manifest digesting and admission control for executable units, attached optional integrity metadata to manifests, rejected untrusted manifests before execution with audit evidence, preserved backward compatibility for legacy runner doubles, and fixed the stale approval test call while narrowing policy secret filtering so non-secret runtime env like `PYTHONPATH` survives execution.
**Tests:** `uv run pytest -q tests` passed; `uv run ruff check src tests` passed
**Artifacts:** `phases/phase-8-runtime-security/artifacts/test-phase8-full-2026-03-27.txt`, `phases/phase-8-runtime-security/artifacts/lint-phase8-full-2026-03-27.txt`
**Blockers:** the only remaining nuance is that the "hardened by default" gate is still marked partial until every untrusted executable-unit path is forced onto hardened isolation without relying on policy/resource hints
**Next:** if you want to fully close Phase 8, make wrapped/project executable units opt into hardened isolation even when their manifests do not declare explicit resource constraints

### 2026-03-30 17:26 — Phase 10 workflow persistence red baseline
**Phase/Tasks:** 10-01
**Status:** in-progress
**What:** added `tests/studio/test_workflows_repository.py` covering Studio workflow metadata tables, draft-head lookup, workspace-scoped reads, and lease conflict behavior for the new `zeroth.studio` package.
**Tests:** `uv run pytest tests/studio/test_workflows_repository.py -q` failed during collection with `ModuleNotFoundError: No module named 'zeroth.studio'`
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-01-red-2026-03-30.txt`
**Blockers:** the Studio package, repositories, and lease services under `src/zeroth/studio/` do not exist yet
**Next:** implement the Studio models, workflow repository/service, and lease repository/service to satisfy the new scoped persistence tests

### 2026-03-30 17:32 — Phase 10 Studio workflow persistence implemented
**Phase/Tasks:** 10-01
**Status:** completed
**What:** added the `src/zeroth/studio/` package with strict workflow and lease models, SQLite repositories for workflow metadata/draft heads and workflow leases, and services that compose the new Studio scope boundary with the existing `GraphRepository` so draft graph content stays in `graph_versions`.
**Tests:** `uv run pytest tests/studio/test_workflows_repository.py -q` passed
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-01-green-2026-03-30.txt`
**Blockers:** none
**Next:** generate the phase summary, update planning state, and advance to the next Phase 10 plan

### 2026-03-30 17:00 — Phase 10 Studio API red baseline
**Phase/Tasks:** 10-02
**Status:** in-progress
**What:** added `tests/studio/test_studio_app.py` to pin the dedicated Studio authoring bootstrap/app surface, including workflow list/create/detail routes, workspace-scoped lease routes, and explicit 401/403/404/409 behavior for anonymous, reviewer, unscoped, and foreign-scope callers.
**Tests:** `uv run pytest tests/studio/test_studio_app.py -q` failed during collection with `ModuleNotFoundError: No module named 'zeroth.studio.bootstrap'`
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-02-red-2026-03-30.txt`
**Blockers:** the Studio FastAPI bootstrap and route modules do not exist yet
**Next:** run the new Studio API suite to capture the failing baseline, then implement the authoring app/bootstrap/routes until the tests pass

### 2026-03-30 17:00 — Phase 10 Studio authoring API implemented
**Phase/Tasks:** 10-02
**Status:** in-progress
**What:** added `src/zeroth/studio/bootstrap.py`, `src/zeroth/studio/app.py`, `src/zeroth/studio/workflows_api.py`, and `src/zeroth/studio/sessions_api.py` to expose a dedicated Studio FastAPI surface on top of the existing workflow and lease services, enforce operator/admin workspace-scoped access, and serialize the narrower workflow and lease contracts needed by downstream Studio plans.
**Tests:** `uv run pytest tests/studio/test_studio_app.py -q` passed
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-02-green-2026-03-30.txt`
**Blockers:** none
**Next:** run a focused lint pass on the touched Studio modules, then commit the implementation and generate the phase summary/state updates

### 2026-03-30 18:00 — Phase 10 authoring validation red baseline
**Phase/Tasks:** 10-05
**Status:** in-progress
**What:** added `tests/studio/test_validation_api.py` covering lease-protected draft saves, scope-aware validation against persisted drafts, and slash-safe contract lookup for `contract://input`.
**Tests:** `uv run pytest tests/studio/test_validation_api.py -q` failed with missing `/draft`, `/validate`, and `/studio/contracts/{contract_ref:path}` behavior
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-05-red-2026-03-30.txt`
**Blockers:** the workflow service has no draft update flow yet, and the Studio app does not register validation or contract lookup routes
**Next:** implement the scoped draft save path, register the validation API, and rerun the focused suite until it passes

### 2026-03-30 18:00 — Phase 10 authoring validation implemented
**Phase/Tasks:** 10-05
**Status:** completed
**What:** added scoped draft-save support in `src/zeroth/studio/workflows/service.py`, exposed `PUT /studio/workflows/{workflow_id}/draft` in `src/zeroth/studio/workflows_api.py`, registered `src/zeroth/studio/validation_api.py` from the Studio app for persisted draft validation and slash-safe contract lookup, and updated the Studio API tests for the expanded workflow detail payload.
**Tests:** `uv run pytest tests/studio/test_validation_api.py -q` passed; `uv run pytest tests/studio/test_validation_api.py tests/studio/test_studio_app.py -q` passed
**Artifacts:** `phases/phase-10-studio-shell-workflow-authoring/artifacts/test-10-05-green-2026-03-30.txt`
**Blockers:** none
**Next:** generate the plan summary, update planning state, and advance to the next Phase 10 plan

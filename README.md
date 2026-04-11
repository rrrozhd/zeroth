# Zeroth

A governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services.

Zeroth treats an agentic application as an **explicit executable graph** rather than an opaque prompt chain. Every node boundary is typed, every executable unit runs inside a hardened sandbox, memory is attachable and shareable, audits are tamper-evident and recorded per node, and every LLM call is cost-attributed in flight. The result is a system you can reason about, govern, operate, and deploy with confidence.

Zeroth is built on top of **[GovernAI](https://github.com/rrrozhd/governai)** as the foundational runtime for governed agent orchestration, and forwards per-call cost events to **Regulus** via the [`econ-instrumentation-sdk`](https://pypi.org/project/econ-instrumentation-sdk/) for economic auditing.

---

## Documentation

Full documentation lives at **<https://rrrozhd.github.io/zeroth/>** —
start with the [Getting Started tutorial](https://rrrozhd.github.io/zeroth/tutorials/getting-started/)
or the [Governance Walkthrough](https://rrrozhd.github.io/zeroth/tutorials/governance-walkthrough/).

---

## Install

```bash
pip install zeroth-core
```

Optional extras pull in swappable backends (base install stays minimal):

```bash
pip install "zeroth-core[memory-pg]"     # Postgres + pgvector memory backend
pip install "zeroth-core[memory-chroma]" # Chroma memory backend
pip install "zeroth-core[memory-es]"     # Elasticsearch memory backend
pip install "zeroth-core[dispatch]"      # Durable worker queue (redis + arq)
pip install "zeroth-core[sandbox]"       # Sandbox sidecar marker
pip install "zeroth-core[all]"           # Everything above
```

Available extras: `memory-pg`, `memory-chroma`, `memory-es`, `dispatch`, `sandbox`, `all`.

---

## Why Zeroth?

Most agent frameworks prioritize getting something working quickly. Zeroth prioritizes getting something **working correctly** — with governance, economic accountability, runtime security, and operational control built in from day one.

| What Zeroth **is** | What Zeroth **is not** |
|---|---|
| A medium-code platform for governed agentic backends | A generic no-code automation tool |
| A graph-based runtime for typed multi-agent systems | A chat UI builder |
| A controlled execution platform for code-backed workflows | A prompt playground |
| A deployment environment that ships workflows as API services | An ungoverned autonomous agent sandbox |
| A cost-attributed, tenant-budget-enforced LLM runtime | A black-box spend sink |

---

## Key Features

- **Provider-agnostic agent runtime** — route to OpenAI, Anthropic, and 100+ other providers through a single LiteLLM-backed adapter, with provider-agnostic structured output, MCP tool discovery, and a GovernAI-managed adapter for governed model access.
- **Economic audits & budget enforcement** — every LLM call is wrapped by an `InstrumentedProviderAdapter` that estimates USD cost via LiteLLM pricing, emits a per-call cost event to Regulus, and attaches `cost_usd` to the response. A `BudgetEnforcer` does pre-execution tenant-budget checks against Regulus (TTL-cached, fail-open) so runaway loops can be stopped before they start.
- **Tamper-evident governance** — append-only audit chains, per-run and per-deployment evidence bundles with policy/tool/approval/memory lineage, and signed deployment snapshot attestations for external verification.
- **Hardened runtime security** — sandbox sidecar with resource ceilings and filesystem boundaries, policy-derived enforcement of network/timeout/secret/side-effect constraints, executable-unit integrity checks (digests + signed metadata), and reference-based secrets with at-rest protection.
- **Durable control plane** — lease-based run dispatch, restart-safe recovery for approvals and thread continuation, bounded concurrency and backpressure per tenant, dead-letter queues, Prometheus metrics (queue depth, run latency, approval wait time), correlation-ID tracing, and admin controls for interrupt/cancel/inspect.
- **Multi-tenant identity** — pluggable API-key and JWT bearer auth, `ActorIdentity`/`AuthenticatedPrincipal` scoped per tenant and workspace, `OPERATOR`/`REVIEWER`/`ADMIN` roles enforced on every route and stamped on every run, approval, and audit record.
- **Outbound webhooks** — HMAC-SHA256-signed event delivery for run lifecycle, approval, and cost-threshold events, with at-least-once semantics, retry-with-backoff, dead-letter handling, and a fully audited delivery history.

---

## Key Concepts

### Graphs

A **graph** is your application. It defines how agents, executable units, and approval steps connect and interact. Graphs can be cyclic, support branching conditions, and are executed asynchronously.

### Three Node Types

Zeroth keeps its primitives minimal. Every graph is composed from just three node types:

- **Agent** — an AI-powered node backed by an LLM provider, with optional tool attachments, MCP tool discovery, and memory connectors
- **Executable Unit** — a sandboxed unit of work (Python code, shell scripts, commands, or full projects) that handles transformations, integrations, routing, and any deterministic processing
- **Human Approval** — a pause point where a human must review and approve before execution continues

### Contracts

Node inputs and outputs are defined by **contracts** — Pydantic-based schemas that are validated at every node boundary. Contracts are versioned in a registry and compile to GovernAI tool/step adapters. Type errors are caught at the edge between nodes, not buried deep inside a run.

### Memory

Agents can optionally attach **memory connectors** for persistent state. Multiple agents can share the same connector instance (and therefore share memory), or each agent can have its own. Built-in backends include local storage, Postgres + pgvector, Chroma, and Elasticsearch, with key-value, thread-scoped, and run-ephemeral stores.

### Threads and Runs

A **run** is a single execution of a graph. A **thread** groups related runs together for conversation continuity. Stateful agents resume their context across runs through a stable `thread_id`, so agents can maintain long-running conversations without treating every invocation as stateless. Run state subclasses GovernAI's `RunState` and is checkpointed durably.

### Governance

Zeroth enforces governance at multiple layers:

- **Policy** — capability-based rules controlling what agents can do (network access, file writes, memory access, secret usage), with policy-derived runtime enforcement rather than log-only warnings
- **Guardrails** — rate limiting, quota enforcement, bounded concurrency, backpressure, and dead-letter queues for repeated failures
- **Audit** — per-node, append-only, tamper-evident event tracking with secret redaction, timeline assembly, correction/supersession semantics, and evidence export
- **Approvals** — human-in-the-loop gates with principal-attributed decision tracking
- **Secrets** — reference-based, resolved from a provider abstraction, protected at rest, and automatically redacted from logs, checkpoints, approvals, and audits
- **Attestations** — deployment snapshot digests and attestation payloads for external verification of graph + pinned contract state

### Economics

The `zeroth.core.econ` subsystem costs every LLM call in flight and forwards events to Regulus:

- **`InstrumentedProviderAdapter`** — wraps any provider adapter to emit cost events and enrich responses with `cost_usd` / `cost_event_id`
- **`CostEstimator`** — converts `(model, prompt_tokens, completion_tokens)` to USD via LiteLLM's pricing table
- **`BudgetEnforcer`** — pre-execution TTL-cached check against Regulus' tenant KPIs; fail-open on outage so observability problems never block production
- **`RegulusClient`** — thin wrapper around the Regulus `InstrumentationClient` for transport, auth, and dashboarding

### Identity & Multi-Tenancy

Every external request is authenticated and attributed. `ServiceAuthConfig` accepts API-key and JWT bearer credentials, minting an `AuthenticatedPrincipal` scoped to a tenant and workspace. Roles (`OPERATOR`, `REVIEWER`, `ADMIN`) are enforced on every route, and the resulting `ActorIdentity` is stamped on every run, approval decision, and `NodeAuditRecord` — so "who did this?" has exactly one answer across the runtime.

### Webhooks

Tenants subscribe URLs to event types (run lifecycle, approvals, cost thresholds). `WebhookService` emits signed payloads, a background delivery worker retries with backoff, and every attempt is persisted to a subscription-scoped history with dead-letter escalation after `max_attempts`.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│                       Service Layer                        │
│          (FastAPI async API wrapper + auth + admin)        │
├────────────────────────────────────────────────────────────┤
│                       Orchestrator                         │
│          (graph traversal, node dispatch, branching)       │
├──────────────┬──────────────┬──────────────┬───────────────┤
│    Agent     │  Executable  │    Human     │   Conditions  │
│   Runtime    │    Units     │   Approvals  │  & Branching  │
│ (LiteLLM /   │  (sandbox    │              │               │
│  GovernAI /  │   sidecar)   │              │               │
│    MCP)      │              │              │               │
├──────────────┴──────────────┴──────────────┴───────────────┤
│  Contracts │  Mappings  │   Memory   │    Policy    │ Econ │
├────────────┴────────────┴────────────┴──────────────┴──────┤
│  Audit (append-only, tamper-evident)  │  Attestations      │
├─────────────────┬────────────────────┬────────────────────┤
│   Guardrails    │   Secrets (ref)    │    Observability    │
│ (rate/quotas/   │  (provider-backed, │  (metrics, tracing, │
│  DLQ, backpress)│   at-rest protect) │   correlation IDs)  │
├─────────────────┴────────────────────┴────────────────────┤
│    Dispatch (lease queue, worker supervision, recovery)    │
├────────────────────────────────────────────────────────────┤
│   Storage (SQLite/Postgres + Redis + pgvector/Chroma/ES)   │
│   Identity & Auth (API key / JWT, tenant/workspace scope)  │
│                     Webhooks (HMAC, DLQ)                   │
└────────────────────────────────────────────────────────────┘
                             │
                             ▼
                 Regulus (external cost + budget service)
```

Zeroth is implemented as a **modular monolith** — all subsystems live in a single deployable unit but are cleanly separated by domain.

---

## Getting Started

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **Docker** (for sandboxed executable unit execution)
- **Redis** (required for durable dispatch and distributed runtime state in service mode; optional when embedding as a library)

### Installation

```bash
# Clone the repository
git clone https://github.com/rrrozhd/zeroth-core.git
cd zeroth-core

# Install dependencies
uv sync

# Verify installation
uv run python -c "import zeroth.core; print('Zeroth is ready')"
```

### Running Tests

```bash
# Run the full test suite
uv run pytest -v

# Run tests for a specific module
uv run pytest tests/graph/ -v
uv run pytest tests/econ/ -v
```

### Linting and Formatting

```bash
# Check for lint errors
uv run ruff check src/

# Auto-format code
uv run ruff format src/
```

---

## Project Structure

Zeroth uses a PEP 420 namespace layout. The core package ships under `zeroth.core`:

```
src/zeroth/core/
├── agent_runtime/      # LLM provider adapters (LiteLLM, GovernAI), MCP, tools, thread store
├── approvals/          # Human approval workflows, SLA checks, decision tracking
├── audit/              # Append-only, tamper-evident per-node audit events
├── conditions/         # Branch evaluation and traversal logging
├── config/             # Settings and runtime configuration
├── contracts/          # Versioned Pydantic contract registry
├── deployments/        # Immutable graph snapshots, digests, attestations
├── dispatch/           # Lease-based durable run dispatch and worker supervision
├── econ/               # Cost estimation, budget enforcement, Regulus client
├── execution_units/    # Native, wrapped-command, and project unit runtimes
├── graph/              # Workflow DAG structure, validation, persistence
├── guardrails/         # Rate limiting, quotas, backpressure, dead-letter queues
├── identity/           # Actor/principal models, roles, tenant scoping
├── mappings/           # Data flow definitions between graph nodes
├── memory/             # Local, pgvector, Chroma, Elasticsearch connectors
├── migrations/         # Alembic schema migrations
├── observability/      # Prometheus metrics, correlation IDs, tracing
├── orchestrator/       # Core workflow execution engine
├── policy/             # Capability-based access control and runtime enforcement
├── runs/               # Run and thread state persistence (GovernAI-aligned)
├── sandbox_sidecar/    # Hardened sandbox executor
├── secrets/            # Reference-based secret providers and redaction
├── service/            # FastAPI HTTP API, auth, lifespan, admin routes
├── storage/            # SQLite/Postgres, Redis, encryption, repositories
└── webhooks/           # Outbound event delivery, HMAC signing, DLQ
```

---

## Executable Unit Modes

Zeroth supports three ways to define executable units:

| Mode | Description | Use Case |
|---|---|---|
| **Native Unit** | Code written directly in the platform | Quick transformations, lightweight logic |
| **Wrapped Command** | Existing script, binary, or command with a manifest | Integrating existing tools without rewriting them |
| **Project Unit** | Uploaded project/archive with build + run manifest | Complex workloads with dependencies |

All executable units run inside sandboxed environments with resource constraints, cached environment reuse, integrity verification (digests + signed metadata), and admission control that rejects untrusted definitions with an audit trail.

---

## Design Principles

Zeroth optimizes for:

- **Explicitness over hidden magic** — every connection, mapping, policy, and cost event is visible and inspectable
- **Governance over permissive flexibility** — agents operate within declared capabilities, enforced at runtime, not just logged
- **Economic accountability** — every LLM call is costed and budget-checked before tenants ever see a bill
- **Manageability over novelty** — production operations come first
- **Compatibility with existing code** — wrap what you have, don't rewrite it
- **Provider agnosticism** — no lock-in to any single LLM vendor; swap via a model string
- **Auditability over opaque orchestration** — per-node, append-only, tamper-evident audit trails, not monolithic logs
- **Explicit state persistence over hidden in-memory behavior** — thread-based continuity you can inspect and reason about

---

## Studio

Zeroth's canvas UI for authoring and inspecting workflows lives in a separate repo:

**[rrrozhd/zeroth-studio](https://github.com/rrrozhd/zeroth-studio)** — Vue 3 + Vue Flow frontend that speaks to `zeroth-core` over HTTP.

The studio was split out in v3.0 Phase 29 to let the two projects ship on independent release cadences. A cross-repo [compatibility matrix](https://github.com/rrrozhd/zeroth-studio#compatibility) documents which studio versions pair with which core versions.

---

## License

See the [LICENSE](LICENSE) file for details.

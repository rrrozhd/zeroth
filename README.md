# Zeroth

A governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services.

Zeroth treats an agentic application as an **explicit executable graph** rather than an opaque prompt chain. Every node boundary is typed, every executable unit runs inside a governed sandbox, memory is attachable and shareable, and audits are recorded per node. The result is a system you can reason about, govern, and deploy with confidence.

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
pip install "zeroth-core[dispatch]"      # Distributed worker (redis + arq)
pip install "zeroth-core[sandbox]"       # Sandbox sidecar marker
pip install "zeroth-core[all]"           # Everything above
```

Available extras: `memory-pg`, `memory-chroma`, `memory-es`, `dispatch`, `sandbox`, `all`.

---

## Why Zeroth?

Most agent frameworks prioritize getting something working quickly. Zeroth prioritizes getting something **working correctly** — with governance, auditability, and operational control built in from day one.

| What Zeroth **is** | What Zeroth **is not** |
|---|---|
| A medium-code platform for governed agentic backends | A generic no-code automation tool |
| A graph-based runtime for typed multi-agent systems | A chat UI builder |
| A controlled execution platform for code-backed workflows | A prompt playground |
| A deployment environment that ships workflows as API services | An ungoverned autonomous agent sandbox |

---

## Key Concepts

### Graphs

A **graph** is your application. It defines how agents, executable units, and approval steps connect and interact. Graphs can be cyclic, support branching conditions, and are executed asynchronously.

### Three Node Types

Zeroth keeps its primitives minimal. Every graph is composed from just three node types:

- **Agent** — an AI-powered node backed by an LLM provider, with optional tool attachments and memory connectors
- **Executable Unit** — a sandboxed unit of work (Python code, shell scripts, commands, or full projects) that handles transformations, integrations, routing, and any deterministic processing
- **Human Approval** — a pause point where a human must review and approve before execution continues

### Contracts

Node inputs and outputs are defined by **contracts** — Pydantic-based schemas that are validated at every node boundary. This means type errors are caught at the edge between nodes, not buried deep inside a run.

### Memory

Agents can optionally attach **memory connectors** for persistent state. Multiple agents can share the same connector instance (and therefore share memory), or each agent can have its own. Memory types include key-value, thread-scoped, and run-ephemeral stores.

### Threads and Runs

A **run** is a single execution of a graph. A **thread** groups related runs together for conversation continuity. Stateful agents resume their context across runs through a stable `thread_id`, so agents can maintain long-running conversations without treating every invocation as stateless.

### Governance

Zeroth enforces governance at multiple layers:

- **Policy** — capability-based rules controlling what agents can do (network access, file writes, memory access, secret usage)
- **Guardrails** — rate limiting, quota enforcement, and dead-letter queues for failed operations
- **Audit** — per-node event tracking with secret redaction, timeline assembly, and evidence summaries
- **Approvals** — human-in-the-loop gates with decision tracking
- **Secrets** — resolved from secure providers and automatically redacted from logs

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                    Service Layer                      │
│              (FastAPI async API wrapper)              │
├──────────────────────────────────────────────────────┤
│                    Orchestrator                       │
│      (graph traversal, node dispatch, branching)     │
├────────────┬────────────┬────────────┬───────────────┤
│   Agent    │ Executable │   Human    │  Conditions   │
│  Runtime   │   Units    │ Approvals  │  & Branching  │
├────────────┴────────────┴────────────┴───────────────┤
│  Contracts │  Mappings  │  Memory    │    Policy     │
├──────────────────────────────────────────────────────┤
│  Audit  │  Guardrails  │  Secrets  │  Observability │
├──────────────────────────────────────────────────────┤
│  Storage (SQLite + Redis)  │  Identity & Auth        │
└──────────────────────────────────────────────────────┘
```

Zeroth is implemented as a **modular monolith** — all subsystems live in a single deployable unit but are cleanly separated by domain.

---

## Getting Started

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **Docker** (for sandboxed executable unit execution)
- **Redis** (for distributed runtime state; optional for local development)

### Installation

```bash
# Clone the repository
git clone https://github.com/rrrozhd/zeroth.git
cd zeroth

# Install dependencies
uv sync

# Verify installation
uv run python -c "import zeroth; print('Zeroth is ready')"
```

### Running Tests

```bash
# Run the full test suite
uv run pytest -v

# Run tests for a specific module
uv run pytest tests/graph/ -v
uv run pytest tests/contracts/ -v
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

```
src/zeroth/
├── agent_runtime/      # Agent execution, LLM providers, tool attachments
├── approvals/          # Human approval workflows and decision tracking
├── audit/              # Per-node event tracking, redaction, evidence
├── conditions/         # Branch evaluation and traversal logging
├── contracts/          # Pydantic-based schema registration and versioning
├── deployments/        # Immutable graph snapshots and version management
├── dispatch/           # Durable run dispatch and worker supervision
├── execution_units/    # Sandboxed code execution (Docker, Python, shell)
├── graph/              # Workflow DAG structure and persistence
├── guardrails/         # Rate limiting, quotas, dead-letter queues
├── identity/           # Authentication, principals, roles, scoping
├── mappings/           # Data flow definitions between graph nodes
├── memory/             # Persistent agent memory connectors
├── observability/      # Metrics, correlation IDs, structured logging
├── orchestrator/       # Core workflow execution engine
├── policy/             # Capability-based access control
├── runs/               # Run and thread state persistence
├── secrets/            # Secret resolution and redaction
├── service/            # FastAPI HTTP API and bootstrap
└── storage/            # SQLite, Redis, migrations, encryption
```

---

## Executable Unit Modes

Zeroth supports three ways to define executable units:

| Mode | Description | Use Case |
|---|---|---|
| **Native Unit** | Code written directly in the platform | Quick transformations, lightweight logic |
| **Wrapped Command** | Existing script, binary, or command with a manifest | Integrating existing tools without rewriting them |
| **Project Unit** | Uploaded project/archive with build + run manifest | Complex workloads with dependencies |

All executable units run inside sandboxed environments with resource constraints, cached environment reuse, and integrity verification.

---

## Design Principles

Zeroth optimizes for:

- **Explicitness over hidden magic** — every connection, mapping, and policy is visible and inspectable
- **Governance over permissive flexibility** — agents operate within declared capabilities
- **Manageability over novelty** — production operations come first
- **Compatibility with existing code** — wrap what you have, don't rewrite it
- **Auditability over opaque orchestration** — per-node audit trails, not monolithic logs
- **Explicit state persistence over hidden in-memory behavior** — thread-based continuity you can inspect and reason about

---

## Studio

Zeroth's canvas UI for authoring and inspecting workflows lives in a separate repo:

**[rrrozhd/zeroth-studio](https://github.com/rrrozhd/zeroth-studio)** — Vue 3 + Vue Flow frontend that speaks to `zeroth-core` over HTTP.

The studio was split out in v3.0 Phase 29 to let the two projects ship on independent release cadences. A cross-repo [compatibility matrix](https://github.com/rrrozhd/zeroth-studio#compatibility) documents which studio versions pair with which core versions.

---

## License

See the [LICENSE](LICENSE) file for details.

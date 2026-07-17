<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/zeroth-logo-dark.svg">
    <img src="docs/assets/logo/zeroth-logo.svg" alt="Zeroth" width="260">
  </picture>
</p>

A governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services.

Zeroth treats an agentic application as an **explicit executable graph** rather than an opaque prompt chain. Every node boundary is typed, executable units can run inside a hardened Docker/sidecar sandbox (the default local backend is for development), memory is attachable and shareable, and audits are recorded per node. The result is a system you can reason about, govern, and deploy with confidence.

---

## Documentation

Full documentation lives at **<https://rrrozhd.github.io/zeroth-core/>** —
start with the [Getting Started tutorial](https://rrrozhd.github.io/zeroth-core/tutorials/getting-started/)
or the [Governance Walkthrough](https://rrrozhd.github.io/zeroth-core/tutorials/governance-walkthrough/).

---

## Quickstart

One command clones the repo, installs everything, and serves a working demo
deployment — a deployed Q&A graph on `http://127.0.0.1:8000` with the web
console at `/console/`:

```bash
curl -fsSL https://raw.githubusercontent.com/rrrozhd/zeroth-core/main/scripts/quickstart.sh | bash
```

(Prefer to read before you run? `curl -fsSLO …/quickstart.sh`, inspect it, then
`bash quickstart.sh`. From an existing checkout just run `./scripts/quickstart.sh`.)

The script installs [uv](https://docs.astral.sh/uv/) if missing, provisions
Python 3.12, builds the web console when Node 20+ is available, and starts the
service. Run from a checkout it prompts for an `OPENAI_API_KEY`; piped through
`bash` (the one-liner above) there is no TTY to prompt on, so it starts without
one — export `OPENAI_API_KEY` beforehand for live answers. Either way the key is
optional: without one you can still explore the console and Studio. Open
**<http://127.0.0.1:8000/console/>** and connect with the demo key
`demo-operator-key` — the console's Guide page and workflow templates take it
from there.

No clone needed — the pip install alone can serve a runnable demo:

```bash
pip install zeroth-core
zeroth-core seed-demo   # creates schema + a deployed single-agent graph;
                        # prints the export + curl commands for your first run
zeroth-core serve
```

Or containerized — seed once, then serve (the image's default command is
`serve` only, so an unseeded container has no demo deployment):

```bash
docker build -t zeroth-core .
docker run -v zeroth-data:/data zeroth-core zeroth-core seed-demo   # seed once
docker run -p 8000:8000 -v zeroth-data:/data zeroth-core            # serve
```

(see `Dockerfile` and `docker-compose.yml`).

---

## Install

```bash
pip install zeroth-core
```

Optional extras pull in swappable backends (base install stays minimal):

```bash
pip install "zeroth-core[console]"       # Bundled web console UI (no Node needed)
pip install "zeroth-core[memory-pg]"     # Postgres + pgvector memory backend
pip install "zeroth-core[memory-chroma]" # Chroma memory backend
pip install "zeroth-core[memory-es]"     # Elasticsearch memory backend
pip install "zeroth-core[dispatch]"      # Distributed worker (redis + arq)
pip install "zeroth-core[sandbox]"       # Sandbox sidecar marker
pip install "zeroth-core[otel]"          # OpenTelemetry trace/metric export
pip install "zeroth-core[regulus]"       # Bundled economic control plane backend
pip install "zeroth-core[all]"           # Everything above except console
```

Available extras: `console`, `memory-pg`, `memory-chroma`, `memory-es`, `dispatch`, `sandbox`, `otel`, `regulus`, `all`.

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

A **graph** is your application. It defines how agents, executable units, and approval steps connect and interact. Graphs can be cyclic, support branching conditions, and are executed asynchronously. A cyclic path must declare a loop safeguard — either set `max_visits_per_edge` in the graph's execution settings, or mark the looping edge's condition `allow_cycle_traversal` — otherwise validation rejects the graph rather than letting it spin.

### Node Types

Zeroth keeps its primitives minimal. Every graph is composed from a small set of node types:

- **Entrypoint** — where a run starts; its contract is the workflow's public input shape, validated before anything executes
- **Agent** — an AI-powered node backed by an LLM provider, with optional memory connectors and tool attachments (other graph units can be attached as callable tools)
- **Executable Unit** — a sandboxed unit of work (Python code, shell scripts, commands, or full projects) that handles transformations, integrations, routing, and any deterministic processing
- **Human Approval** — a pause point where a human must review and approve before execution continues
- **Retrieval** — queries a memory/knowledge connector and passes the top matches downstream (the grounding step in a RAG flow)
- **Subgraph** — invokes another published graph as a single step, keeping workflows small and composable

### Contracts

Node inputs and outputs are defined by **contracts** — Pydantic-based schemas that are validated at every node boundary. This means type errors are caught at the edge between nodes, not buried deep inside a run.

### Memory

Agents can optionally attach **memory connectors** for persistent state. Multiple agents can share the same connector instance (and therefore share memory), or each agent can have its own. Memory types include key-value, thread-scoped, and run-ephemeral stores, plus vector/knowledge backends (pgvector, Chroma, Elasticsearch) — the vector backends are what a Retrieval node queries to ground a RAG flow.

### Threads and Runs

A **run** is a single execution of a graph. A **thread** groups related runs together for conversation continuity. Stateful agents resume their context across runs through a stable `thread_id`, so agents can maintain long-running conversations without treating every invocation as stateless.

### Governance

Zeroth enforces governance at multiple layers:

- **Policy** — capability-based rules controlling what agents can do (network access, file writes, memory access, secret usage). Enforcement is **on by default and fail-closed**: a served node that invokes a tool or touches memory without declaring the matching capability is *denied* (agent tool calls and memory reads/writes are behaviorally gated, not merely audited); an agent-invoked executable unit runs under the calling agent's enforcement envelope, so the sandbox network/secret gate applies to it too. Behavioral **network and filesystem** isolation for executable units requires the Docker or sidecar backend. Under the **strict** posture the local backend is refused outright — a host subprocess can never provide hardened isolation; under the **standard** posture it is refused for any unit that requires hard isolation (network access or a resource limit), rather than running it unconstrained. Turn enforcement off (capabilities become advisory) with `ZEROTH_POLICY__ENFORCE_CAPABILITIES=false`.
- **Guardrails** — rate limiting, quota enforcement, and dead-letter queues for failed operations, plus opt-in content safety on agent responses (blocklist and PII filters with a configurable enforcement mode)
- **Budgets** — per-tenant spend caps enforced through the bundled economic control plane. Enforcement requires the `regulus` extra (included in `[all]`): with it installed, the plane is mounted in-process at `/regulus` with no env flags required, so a default deploy reaches it over the app's own ASGI transport, not a separate host, and per-tenant caps trip with per-node cost attribution from the first run. A fresh deploy auto-generates a strong **ephemeral per-process signing secret** for the mount, so it boots with no configuration; set `ECP_JWT_SECRET` to a persistent value for **multi-worker or persistent deployments**. Prefer an external Regulus instead? Point at it with `ZEROTH_REGULUS__BASE_URL`. Note the split: cost/event tracking always follows `BASE_URL`, but cap *enforcement* only follows it on a bare install — with the `regulus` extra installed the in-process mount takes precedence for enforcement, so disable the plane (`ZEROTH_REGULUS__ENABLED=false`) if you want enforcement to go to an external host. Turn the plane off entirely with `ZEROTH_REGULUS__ENABLED=false`. The pre-LLM tenant check **fails open by default** — a control-plane outage never blocks a run, it is logged at `WARNING` and the cap simply isn't enforced for that call; flip to **fail-closed** (deny on backend error) with `ZEROTH_REGULUS__FAIL_CLOSED=true`. The tenant check is eventually consistent: it sees spend recorded *before* the current call (spend-to-date), so a single run can overshoot within one cycle. For a tighter, control-plane-independent guard, set a per-run cumulative ceiling with `ZEROTH_REGULUS__PER_RUN_CAP_USD` (USD) — enforced locally from the run's own audit cost, so it works even with the control plane disabled, halting a run on the next node once its accumulated cost crosses the cap. On a bare install (`pip install zeroth-core`, no extra) there is no enforcement backend at all — the pre-LLM tenant check fails open and caps are **not** enforced until you install the extra or point at an external plane.
- **Audit** — per-node event tracking with secret redaction, timeline assembly, and evidence summaries, recorded on an append-only **tamper-evident hash chain** (each record pins its predecessor's SHA-256 digest) with optional **keyed signing** over each digest, so history can be verified — and tampering detected — after the fact. Right-to-erasure is supported without breaking the chain: PII is crypto-erased behind per-field commitments, and a record that claims erasure while still carrying PII fails verification
- **Approvals** — human-in-the-loop gates with decision tracking
- **Secrets** — resolved from secure providers and automatically redacted from logs

### Platform Extensions

Six further subsystems support production agentic workflows:

- **Resilient HTTP Client** — Platform-provided async HTTP with retry, circuit breaking, and connection pooling for external API calls
- **Prompt Templates** — Versioned prompt template registry with Jinja2 sandboxed rendering and automatic secret redaction in audit records
- **Context Window Management** — Per-agent token tracking with automatic compaction (truncation, observation masking, or LLM summarization) when thresholds are reached
- **Parallel Fan-Out/Fan-In** — Spawn N concurrent branches from a node's output with isolated execution contexts, deterministic merge, and per-branch cost attribution
- **Subgraph Composition** — Reference published graphs as nested nodes with inherited governance, configurable thread sharing, and approval propagation
- **Artifact Store** — Externalize large binary outputs from nodes; retrieve via `GET /v1/artifacts/{id}`

REST endpoints for artifact retrieval (`GET /v1/artifacts/{id}`) and template management (`GET/POST /v1/templates`, `GET /v1/templates/{name}`, `DELETE /v1/templates/{name}/{version}`) are available under `/v1/`; prompt templates are also manageable from the console's Templates page.

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
├────────────┬────────────┬────────────┬───────────────┤
│  Parallel  │  Subgraph  │ Templates  │   Artifacts   │
│  Execution │ Composition│            │               │
├────────────┴────────────┼────────────┴───────────────┤
│  Context Window Mgmt    │  Resilient HTTP Client     │
├──────────────────────────────────────────────────────┤
│  Audit  │  Guardrails  │  Secrets  │  Observability │
├────────────────────────┬─────────────────────────────┤
│  Retention & Erasure   │  Provenance Signing         │
├────────────────────────┴─────────────────────────────┤
│  Econ Plane (cost attribution, tenant budget caps)   │
├──────────────────────────────────────────────────────┤
│  Storage (SQLite / Postgres + Redis) │ Identity & Auth│
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
git clone https://github.com/rrrozhd/zeroth-core.git
cd zeroth-core

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
src/zeroth/core/
├── agent_runtime/      # Agent execution, LLM providers, tool attachments
├── approvals/          # Human approval workflows and decision tracking
├── artifacts/          # Artifact externalization and retrieval
├── audit/              # Per-node event tracking, redaction, evidence
├── cli.py              # zeroth-core CLI (serve, seed-demo, migrate)
├── conditions/         # Branch evaluation and traversal logging
├── config/             # Runtime settings and configuration reference
├── context_window/     # Token tracking and context compaction
├── contracts/          # Pydantic-based schema registration and versioning
├── demos/              # Demo graph fixtures backing the seed-demo CLI command
├── deployments/        # Immutable graph snapshots and version management
├── dispatch/           # Durable run dispatch and worker supervision
├── econ/               # Cost estimation, budget enforcement, econ integration
├── eval/               # Agent evaluation harness: datasets, scorers, CI gate
├── examples/           # Runnable quickstart and demo service
├── execution_units/    # Sandboxed code execution (Docker, Python, shell)
├── governed/           # Vendored governed-runtime primitives (absorbed governai): memory types, tool contracts, run-state, audit emitters
├── graph/              # Workflow DAG structure and persistence
├── guardrails/         # Rate limiting, quotas, dead-letter queues
├── http/               # Resilient async HTTP client with retry and circuit breaking
├── identity/           # Authentication, principals, roles, scoping
├── mappings/           # Data flow definitions between graph nodes
├── memory/             # Persistent agent memory connectors
├── migrations/         # Schema migrations applied at boot
├── observability/      # Metrics, correlation IDs, structured logging
├── orchestrator/       # Core workflow execution engine
├── parallel/           # Fan-out/fan-in concurrent branch execution
├── policy/             # Capability-based access control
├── rag/                # Document ingestion for retrieval (chunk + embed)
├── retention/          # Per-tenant retention TTLs, legal holds, right-to-erasure
├── runs/               # Run and thread state persistence
├── sandbox_sidecar/    # Sidecar sandbox backend
├── secrets/            # Secret resolution and redaction
├── service/            # FastAPI HTTP API, console mount, bootstrap
├── signing/            # Keyed provenance signing over audit digests
├── storage/            # SQLite, Postgres, Redis, migrations, encryption
├── subgraph/           # Nested graph composition and resolution
├── templates/          # Versioned prompt template registry
└── webhooks/           # Webhook subscriptions, signed delivery, dead-letter
```

The rest of the repo, relative to the root:

```
src/zeroth/
├── core/                          # the tree above
│   └── econ/instrumentation/      # Cost-instrumentation SDK used by core/econ/
└── econ_plane/                    # Economic control plane backend (absorbed Regulus)
frontend/                          # Next.js web console (static export, see below)
```

---

## Executable Unit Modes

Zeroth supports three ways to define executable units:

| Mode | Description | Use Case |
|---|---|---|
| **Native Unit** | Code written directly in the platform | Quick transformations, lightweight logic |
| **Wrapped Command** | Existing script, binary, or command with a manifest | Integrating existing tools without rewriting them |
| **Project Unit** | Uploaded project/archive with build + run manifest | Complex workloads with dependencies |

Executable units run under a configurable sandbox backend — hardened Docker (read-only root, all capabilities dropped, no-new-privileges) or a sidecar, with resource constraints, cached environment reuse, and integrity verification. The default `local` backend runs units as host subprocesses with env filtering only: choose Docker or sidecar for untrusted code.

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

## Web Console

An in-repo web console (`frontend/`) for operating and authoring Zeroth apps —
a Next.js **static export** that talks to the platform's HTTP API.

| | |
|---|---|
| ![Overview — deployment health and getting-started checklist](docs/assets/console/overview.png) | ![Studio — workflow templates and graph list](docs/assets/console/studio.png) |
| ![Graph editor — RAG template on the canvas](docs/assets/console/editor.png) | ![Node editor — inline help and field hints](docs/assets/console/node-editor.png) |

**Start from a template, not a blank canvas.** Studio ships ready-made example
graphs — *Grounded Q&A (RAG)*, *Approval-gated action*, *Tool → Agent
pipeline* — that instantiate as fully editable drafts in one click. Every node
config field carries a hint and example value, each node type explains itself
in the editor, and the built-in **Guide** page covers concepts, the
zero-to-run walkthrough, a node type reference, and an API quickstart. Empty
states across Runs, Approvals, and Audit explain how each view gets populated.

The console is built once and runs in **two modes from the same bundle**:

- **Mounted** — when a build is present, the FastAPI app serves it at
  `/console` on the same origin as the API. One deployment ships both API and
  UI; no CORS, no second host.
- **Standalone** — host the static bundle anywhere and point it at a remote
  API. Set the API base URL + key in the console's *Connect* bar; enable CORS
  on the API (below).

The console reads its API base URL and `X-API-Key` from the browser at runtime
(localStorage), so the same artifact works in both modes.

**What it covers:** an overview/health dashboard with deployments (including
rollback) and a getting-started checklist; runs (submit with example payloads, a
live-polling detail view, `RUN_ADMIN` cancel/interrupt/replay controls, and
ready-made cURL for the deployed API); approvals (approve/reject); per-node
audit; a Cost page covering deployment cost, tenant month-to-date spend against
the budget cap (settable in-console), and portfolio economics; a Retention &
Compliance page for the tenant retention policy, legal holds, and
right-to-erasure requests; prompt templates (register, preview, delete);
integrations (webhook subscriptions and dead-letter replay); connector
administration; a Studio with workflow templates, CRUD, publish/deploy, and a
full-screen React Flow canvas; and an in-console Guide.

> **Studio authoring closes the canvas→run loop.** On a *draft* you can add
> any of the node types above — plus inline Python **code** nodes that execute
> in the sandbox — edit each node's config, draw data edges and agent **tool**
> edges, author JSON-Schema contracts, and pick the entrypoint. Publish the
> draft, deploy it, and run it straight from the canvas with live per-node
> status and past-run replay — no Python required. One caveat, surfaced in the
> deploy dialog too: creating a deployment registers a new version, but serving
> it still needs a service restart — runs from the canvas execute against the
> currently *served* deployment. Published graphs are read-only — *clone to a
> draft* to edit.

```bash
# Easiest: the [console] extra ships the pre-built UI as the zeroth-console
# package — no Node toolchain required. Any Zeroth service then serves it
# at http://<host>/console/ automatically.
pip install "zeroth-core[console]"

# From a source checkout: build the static export yourself (requires Node;
# produces frontend/out/, which takes precedence over the installed package)
cd frontend && npm install && npm run build

# Override the assets location explicitly with ZEROTH_CONSOLE_DIR=/path/to/out

# Standalone dev against a running API on :8000
npm run dev                       # serves http://localhost:3000/console/
# ...and allow that origin on the API:
export ZEROTH_CONSOLE_CORS_ORIGINS="http://localhost:3000"
```

| Env var | Purpose |
| ------- | ------- |
| `ZEROTH_CONSOLE_DIR` | Explicit path to the built `out/` dir (default resolution: `frontend/out` in a checkout, then the installed `zeroth-console` package). |
| `ZEROTH_CONSOLE_CORS_ORIGINS` | Comma-separated origins allowed to call the API cross-origin (standalone mode). Unset = no CORS (mounted mode). |

If no build is present (Python-only install / CI with no Node), the mount is
skipped silently and the API is unaffected. See [frontend/README.md](frontend/README.md)
for development details.

---

## License

See the [LICENSE](LICENSE) file for details.

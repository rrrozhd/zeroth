# Zeroth

Zeroth is an open-source platform for running AI workflows, controlling their
actions, and evaluating changes to cost and outcomes. Its Python runtime and
HTTP APIs cover orchestration, governance, and economic analysis. The console
is optional.

[Source](https://github.com/rrrozhd/zeroth) ·
[SDK on PyPI](https://pypi.org/project/zeroth-sdk/0.1.0a1/) ·
[Releases](https://github.com/rrrozhd/zeroth/releases) ·
[Issues](https://github.com/rrrozhd/zeroth/issues)

## Choose a starting point

| What you want to do | Start here |
|---|---|
| Build a workflow with agents, tools, and typed data | [First graph](tutorials/getting-started/02-first-graph.md) |
| Run an API service with policy and human approvals | [Service mode and approvals](tutorials/getting-started/03-service-and-approval.md) |
| Govern an existing LangGraph application | [Govern LangGraph tools](how-to/cookbook/govern-langgraph-tools.md) |
| Understand workflow costs and outcomes | [Economic debugger](how-to/economic-debugger.md) |
| Compare a model change and retrieve decision reports | [SDK guide on PyPI](https://pypi.org/project/zeroth-sdk/0.1.0a1/) |
| Compare measured usage with provider bills | [Provider-bill reconciliation](how-to/provider-bill-reconciliation.md) |

The [installation guide](tutorials/getting-started/01-install.md) explains the
source setup for the runtime tutorials. These pages describe the current source
tree; not every component has a matching PyPI release.

## Packages and availability

| Package | Role | Current install path |
|---|---|---|
| `zeroth-sdk` | Lightweight remote Python client and wire contracts | Experimental `0.1.0a1` prerelease on PyPI |
| `zeroth-platform` | Runtime, governance, APIs, and economic modules | Source checkout; PyPI release pending |
| `zeroth-console` | Optional static operator UI | Build from the repository; current companion release pending |
| `zeroth-core` | Exact-version compatibility package for the platform | Compatibility release pending; old PyPI `0.1.0` is not the current platform |

To connect to your own deployment with Python 3.12+:

```bash
python -m pip install "zeroth-sdk==0.1.0a1"
```

The SDK requires a project API key and an explicit `base_url` for your economic
API. It installs `httpx` and `pydantic`, not the platform or console. Its APIs
may change before a stable release. Zeroth Cloud, the proposed managed service,
is **not yet available**; an SDK installation does not provision hosting.

## Core capabilities

### Orchestration and contracts

Define agents, code, tools, retrieval, approvals, and nested graphs with typed
inputs and outputs. The runtime supports branching, bounded loops, parallel
execution, checkpoints, and resumable runs. Memory connectors, prompt templates,
and artifacts support state and data across steps.

Read [graph concepts](concepts/graph.md), [executable units](concepts/execution-units.md),
and [memory](concepts/memory.md).

### Governance

Policies, budgets, approvals, and audit records belong to the backend. They
do not depend on using the UI. Enforcement depends on the integration boundary:
a gateway cannot govern tool calls inside an Agent Server without the in-process
adapter. Untrusted executable code needs a Docker or sidecar sandbox rather than
the local subprocess backend.

Read the [governance walkthrough](tutorials/governance-walkthrough.md) and
[LangGraph deployment guide](how-to/deployment/langgraph-release.md).

### Economic analysis and experiments

Attribute measured cost and outcomes to versions, runs, attempts, and cohorts.
Backtest bounded model changes and inspect where evidence is missing. The
experimental model-migration API simulates cost, quality, latency, and critical
errors; checks risk constraints; and retains a recommendation or abstains.
Schedules can refresh decisions, PDFs make them shareable, and randomized rollout
verification supplies observations for later calibration checks.

Recommendations are advisory. Simulation volume is not a substitute for real
evidence, and before/after measurements alone do not establish causality.
Explicit SMTP report delivery is implemented; automatic scheduled email delivery
and broader prompt, retry, capacity, and delivery-time optimization remain future
work. Managed hosting is also a separate, unfinished product step.

Start with the [economic debugger setup](how-to/economic-debugger.md).

## Working locally

From a source checkout matching these docs:

```bash
uv sync --extra regulus
uv run zeroth seed-demo
# Apply the environment exports printed by seed-demo, then:
uv run zeroth serve
```

The local service exposes its API explorer at `http://localhost:8000/docs`.
Provider-backed model calls need a provider key. This demo setup is not a
production deployment recipe; use [local development](how-to/deployment/local-dev.md)
for setup details and the deployment guides before exposing a service.

The optional console adds graph authoring and operator views over the same APIs.
See [local development](how-to/deployment/local-dev.md) for building and connecting it.

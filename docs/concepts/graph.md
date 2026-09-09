# Graph

## What it is

A **graph** is Zeroth's declarative description of a multi-agent workflow: a versioned collection of nodes (steps) and edges (connections) plus the execution settings that govern how it runs. It is the primary artifact you author when building on Zeroth.

## Why it exists

Multi-agent systems quickly collapse into tangles of ad-hoc function calls, implicit state, and hand-rolled control flow. The graph gives you a single, inspectable, versionable object that captures *what* happens, *in what order*, and *under what governance*, while deferring *how* each step runs to the [orchestrator](orchestrator.md). Because a `Graph` is a Pydantic model, it can be validated, diffed, stored, published, archived, and compiled to a `GovernedFlowSpec` for execution — without ever touching running code.

## Where it fits

The graph sits at the center of Zeroth. It is produced by your code (or by the Studio UI), persisted via `GraphRepository`, and handed to the [orchestrator](orchestrator.md) at run time. Nodes reference [agents](agents.md) and [execution units](execution-units.md). Studio-authored control flow uses explicit If and Loop nodes; the lower-level graph API retains condition-bearing edges for legacy and programmatic graphs. Adjacent subsystems — contracts, policy, approvals, audit — attach to the graph through refs on nodes and edges, so the graph is also the bind site for governance.

## Key types

- **`Graph`** — top-level workflow object with nodes, edges, `ExecutionSettings`, lifecycle status, and a `to_governed_flow_spec()` compiler.
- **`Node`** — discriminated union of entrypoint, agent, executable/code, approval, retrieval, HTTP, subgraph, If, and Loop node models; one per step.
- **`Edge`** — directed connection between two nodes, optionally carrying an `EdgeMapping` and a `Condition`.
- **`GraphStatus`** — lifecycle enum (`DRAFT`, `PUBLISHED`, `ARCHIVED`) enforced by `transition_to()`.
- **`GraphRepository`** — persistence layer that stores, versions, and retrieves graphs from a database.

## Node types
Zeroth keeps its primitives minimal. Every graph is composed from a small set of node types:

- **Entrypoint** — where a run starts; its contract is the workflow's public input shape, validated before anything executes
- **Agent** — an AI-powered node backed by an LLM provider, with optional memory connectors and tool attachments (other graph units can be attached as callable tools)
- **Code** — inline Python authored on the canvas and executed through the same immutable, sandboxed executable-unit machinery
- **Executable Unit** — a sandboxed unit of work (Python code, shell scripts, commands, or full projects) that handles transformations, integrations, routing, and any deterministic processing
- **MCP Tool** — one tool on an external MCP (Model Context Protocol) server, frozen at import time (name, description, input schema, and a digest over all three) so it has a contract at publish rather than only at run time, and attached to an agent as a callable tool. The server's command and its capability ceiling live in an operator-owned registry the graph author cannot edit. The call is **at-least-once** — no operation receipt, no replay suppression — which is marked in the audit record rather than implied away
- **Human Approval** — a pause point where a human must review and approve before execution continues
- **If** — an explicit two-way decision node whose condition routes through named `True` and `False` ports
- **Loop** — a bounded retry controller with visible `Repeat`, `Done`, and `Limit` routes and a required maximum retry count
- **Retrieval** — queries a memory/knowledge connector and passes the top matches downstream (the grounding step in a RAG flow)
- **HTTP Request** — a governed private HTTP step with bounded timeouts, retries, response limits, and circuit-breaker behavior
- **Subgraph** — invokes another published graph as a single step, keeping workflows small and composable

## See also

- [Usage Guide: graph](../how-to/graph.md)
- [Concept: orchestrator](./orchestrator.md)
- [Concept: conditions](./conditions.md)

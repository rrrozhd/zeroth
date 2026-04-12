# 2. First graph with an agent, a tool, and an LLM call

Section 1 proved your install works. This section builds the smallest
interesting **governed graph** Zeroth can run: one
[`AgentNode`](../../reference/python-api.md) wired to one
[`ExecutableUnitNode`](../../reference/python-api.md) via a single
`Edge`, driven end-to-end by the in-process `RuntimeOrchestrator`.

A graph is more than an LLM call — it is a versioned, contract-checked,
audited execution plan. Every node has typed inputs and outputs, every
edge records an audit, and every step is governed by policy. The point
of this section is to feel that structure with the minimum possible
code.

## The quickstart helper

The `zeroth.core.examples.quickstart` module ships a tiny helper
`build_demo_graph()` that returns a two-node linear graph — one agent,
one tool — so you can skip the 80 lines of graph assembly and focus on
the run surface.

!!! warning "Tutorial helper, not a stable API"

    `zeroth.core.examples.quickstart` is a **tutorial helper only**. It
    is not a stable public API and may change without a deprecation
    cycle. For real graph authoring, follow the Phase 31+ graph
    authoring guide (once it lands) and build your graph with the
    public `zeroth.core.graph` models directly.

## The example script

`examples/01_first_graph.py` is the full, runnable implementation. It
boots Zeroth in-process against a temporary SQLite database, registers
two contracts, persists and deploys an agent→executable-unit graph,
wires a real `AgentRunner` backed by `LiteLLMProviderAdapter`, and
drives the graph to completion via `orchestrator.run_graph(...)`.

```python title="examples/01_first_graph.py"
--8<-- "01_first_graph.py"
```

Run it (OpenAI this time, so Section 3 can reuse the same key):

```bash
export OPENAI_API_KEY=sk-...
python examples/01_first_graph.py
```

## Expected output

```text
── first-graph summary ─────────────────────────────
  run_id : <uuid>
  status : COMPLETED
  output : {'formatted': '# <Topic>\n\n<body>\n', 'topic': '<topic>'}
```

Without `OPENAI_API_KEY` the script prints a `SKIP` line to stderr and
exits `0`. The same happy-path is exercised on every push to `main` by
the `examples` CI workflow with a repo secret.

## What just happened

1. `run_migrations(...)` created the SQLite schema in a tempdir.
2. `ContractRegistry.register(...)` taught Zeroth about the
   `contract://demo-input` and `contract://demo-output` payload shapes
   the demo graph uses for its node I/O.
3. `GraphRepository.create(build_demo_graph())` persisted the quickstart
   graph snapshot; `DeploymentService.deploy(...)` pinned it as
   deployment `demo-first-graph`.
4. `bootstrap_service(...)` wired up every subsystem — runs,
   approvals, orchestrator, audit, policy, memory — for that single
   deployment.
5. `orchestrator.run_graph(...)` drove the run: agent node → edge →
   tool node → completed, with full audit records written to the DB.

## Next

[→ Section 3: Run in service mode with an approval gate](03-service-and-approval.md)

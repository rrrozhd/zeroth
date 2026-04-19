# 1. Install

**Time budget: under 5 minutes.** This is the gate for the whole
Getting Started tutorial — if you finish this section you have a
working `zeroth-core` install and have made a real LLM call through
it.

## Install the package

```bash
pip install zeroth-core
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv add zeroth-core
```

Optional backends (Postgres, pgvector, Chroma, Elasticsearch, Redis,
Regulus economics) are available via the `[all]` extra and its
component extras; see `pyproject.toml` for the full list. The Getting
Started tutorial runs entirely on the default in-memory SQLite backend,
so you do not need any extras to complete the tutorial.

## Set an API key

The hello example below makes one real LLM call through
[litellm](https://github.com/BerriAI/litellm) using Anthropic. Set your
key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

If you prefer OpenAI (which Section 2 and Section 3 use), export
`OPENAI_API_KEY` instead and edit the `model=` argument in the hello
script — any litellm-supported provider works.

## Run the hello example

The canonical smoke test lives at `examples/00_hello.py`. It builds a
one-node graph, wires a real `AgentRunner` through
`LiteLLMProviderAdapter`, and runs it through the orchestrator. If
this runs end-to-end your install is healthy.

```python title="examples/00_hello.py"
--8<-- "00_hello.py"
```

Run it:

```bash
python examples/00_hello.py
```

Expected output: a single short greeting sentence from the LLM.

If `ANTHROPIC_API_KEY` is unset the script prints a `SKIP` notice to
stderr and exits `0` — that same behaviour keeps CI green on forked
pull requests that do not have secrets configured.

## Next

[→ Section 2: First graph with an agent, a tool, and an LLM call](02-first-graph.md)

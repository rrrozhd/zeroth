# 1. Install

This tutorial uses the full `zeroth-platform` source tree. It installs the
runtime and runs one model call; the lightweight remote SDK is a different
package.

## Install from source

```bash
uv sync --extra regulus
```

Run this from a checkout matching the documentation you are reading, with
Python 3.12+ and [uv](https://docs.astral.sh/uv/) installed. The current platform
release is not yet on PyPI. Only the experimental remote client,
[`zeroth-sdk==0.1.0a1`](https://pypi.org/project/zeroth-sdk/0.1.0a1/), is available
there; installing it does not install the graph runtime used by this tutorial.

Optional backends (Postgres, pgvector, Chroma, Elasticsearch, Redis,
Regulus economics), the web console, and the LangGraph integrations are
available through extras; see `pyproject.toml` for the full list. The `regulus`
extra includes the bundled economic backend for the later service tutorial.
You do not need the console to run the examples.

## Set an API key

The hello example below makes one real LLM call through
[litellm](https://github.com/BerriAI/litellm) using OpenAI
(`openai/gpt-4o-mini`). Set your key:

```bash
export OPENAI_API_KEY=sk-...
```

Any litellm-supported provider works — edit the `model=` argument in the
hello script to switch (e.g. `anthropic/claude-sonnet-5` with
`ANTHROPIC_API_KEY`).

## Run the hello example

The canonical smoke test lives at `examples/00_hello.py` **in the
repository** — the `examples/` directory is not shipped inside the wheel,
so run it from the source checkout used above. To seed a runnable demo service
instead, see [Local development](../../how-to/deployment/local-dev.md).

It builds a one-node graph, wires a real `AgentRunner` through
`LiteLLMProviderAdapter`, and runs it through the orchestrator. If
this runs end-to-end your install is healthy.

```python title="examples/00_hello.py"
--8<-- "00_hello.py"
```

Run it:

```bash
uv run python examples/00_hello.py
```

Expected output: a single short greeting sentence from the LLM.

If `OPENAI_API_KEY` is unset the script prints a `SKIP` notice to
stderr and exits `0` — that same behaviour keeps CI green on forked
pull requests that do not have secrets configured.

## Next

[→ Section 2: First graph with an agent, a tool, and an LLM call](02-first-graph.md)

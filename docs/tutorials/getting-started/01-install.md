# 1. Install

**Time budget: under 5 minutes.** This is the gate for the whole
Getting Started tutorial — if you finish this section you have a
working `zeroth-core` install and have made a real LLM call through
it.

## Install the package

```bash
pip install zeroth-core
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add zeroth-core
```

Optional backends (Postgres, pgvector, Chroma, Elasticsearch, Redis,
Regulus economics), the web console, and the LangGraph integrations are
available through extras; see `pyproject.toml` for the full list. The Getting
Started tutorial runs entirely on the default in-memory SQLite backend,
so you do not need any extras to complete the tutorial.

!!! note "Published package versus current docs"
    This documentation is built from `main`, which can be ahead of the latest
    package on [PyPI](https://pypi.org/project/zeroth-core/). For the exact
    source documented here, use the repository checkout below and run commands
    with `uv run`.

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
so clone the repo to run it:

```bash
git clone https://github.com/rrrozhd/zeroth.git && cd zeroth
uv sync
```

(No clone handy? `zeroth-core seed-demo && zeroth-core serve` gives you a
running demo service from the bare pip install — see
[Local development](../../how-to/deployment/local-dev.md).)

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

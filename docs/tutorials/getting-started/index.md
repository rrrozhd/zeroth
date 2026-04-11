# Getting Started

A linear three-section tutorial. Time budget:

- **<5 minutes to first working output** — complete section 1.
- **<30 minutes end-to-end** — complete all three sections.

By the end you will have installed `zeroth-core` in a clean virtualenv,
built a minimal governed graph with one agent and one tool, and run that
graph both as an embedded library and as a standalone FastAPI service
with a real human-in-the-loop approval gate resolved over HTTP.

## Sections

1. [**Install**](01-install.md) — `pip install zeroth-core`, set
   `ANTHROPIC_API_KEY`, run `examples/hello.py`, see a real LLM call
   complete. This is the <5 minute gate.
2. [**First graph**](02-first-graph.md) — build a minimal graph with one
   agent, one tool, and one LLM call using the
   `zeroth.core.examples.quickstart` helper. Drive it to completion
   in-process via the `RuntimeOrchestrator` — the library-embedded path.
3. [**Service mode & approval**](03-service-and-approval.md) — boot the
   same graph as a FastAPI service with a `HumanApprovalNode`, POST a
   run, and resolve the pending approval via `curl` (and the Python
   equivalent).

Both OpenAI (section 2/3) and Anthropic (section 1) are supported
through [litellm](https://github.com/BerriAI/litellm); the tutorials use
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` respectively. Any
litellm-compatible provider works.

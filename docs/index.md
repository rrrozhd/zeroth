# Zeroth

A governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services.

[Source](https://github.com/rrrozhd/zeroth) ·
[Releases](https://github.com/rrrozhd/zeroth/releases) ·
[PyPI](https://pypi.org/project/zeroth-core/) ·
[Issues](https://github.com/rrrozhd/zeroth/issues) ·
[Changelog](https://github.com/rrrozhd/zeroth/blob/main/CHANGELOG.md)

## Install

Install the latest published release from
[PyPI](https://pypi.org/project/zeroth-core/):

```bash
pip install zeroth-core
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add zeroth-core
```

!!! note "Docs for the current source tree"
    This site is built from the repository's `main` branch, which can be ahead
    of PyPI. To run the exact code documented here, clone
    [rrrozhd/zeroth](https://github.com/rrrozhd/zeroth) and run `uv sync`.

Use the [Getting Started tutorial](tutorials/getting-started/index.md) for a
guided first graph, or jump to [local development](how-to/deployment/local-dev.md)
to run the API and web console.

## Choose Your Path

=== "Embed as library"

    Import Zeroth directly into your Python application. Build a graph
    with one agent, one tool, and one LLM call, then drive it to
    completion inside your own process — no HTTP hop.

    [Start: First graph tutorial →](tutorials/getting-started/02-first-graph.md)

=== "Run as service"

    Boot Zeroth as a standalone FastAPI service, POST runs over HTTP,
    and exercise the governance surface (human approval gate, policy
    block, audit trail) through the real service API.

    [Start: Service mode & approval tutorial →](tutorials/getting-started/03-service-and-approval.md)

New here? The [Getting Started tutorial](tutorials/getting-started/index.md)
walks both paths end-to-end in under 30 minutes.

## Hello, Zeroth

The smallest possible smoke test — install the package, set
`OPENAI_API_KEY`, and run the script below. You should see a one-line
LLM greeting in under 5 minutes.

```python title="examples/00_hello.py"
--8<-- "00_hello.py"
```

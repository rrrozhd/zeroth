# Zeroth

**The economic debugger for production AI workflows.**

Zeroth explains cost and breakage by outcome, workflow version, step,
subject/cohort, and time, then backtests a cost-saving change before rollout.

[Understand the product boundary →](concepts/economic-optimization.md)

[Source](https://github.com/rrrozhd/zeroth) ·
[Releases](https://github.com/rrrozhd/zeroth/releases) ·
[PyPI](https://pypi.org/project/zeroth-core/) ·
[Issues](https://github.com/rrrozhd/zeroth/issues) ·
[Changelog](https://github.com/rrrozhd/zeroth/blob/main/CHANGELOG.md)

## Current availability

The economic-debugger API is self-hostable from this repository. It accepts
tenant-scoped evidence, exposes timeline/cohort/breakage queries, compares exact
workflow versions, and runs bounded model-change backtests. Managed hosting,
production provider credentials, merchant checkout/webhook integration,
organization rollups, and signed proof-of-savings are not implemented product
claims. The internal subscription projection exists but is not yet a way to buy
the service.

[Run the economic debugger API →](how-to/economic-debugger.md)

!!! warning "Standalone SDK release is blocked"
    `packaging/sdk` now has matching authenticated routes, retrieval, and real
    end-to-end coverage. Do not publish or recommend `pip install zeroth-sdk`
    until a supported hosted endpoint and production provider credentials exist.

!!! note "PyPI"
    The published `zeroth-core` package is a stale `0.1.0` placeholder (verified
    2026-08-24). It is also the preserved local platform, not the lean customer
    SDK. Do not install it for the current source tree.

!!! note "Docs for the current source tree"
    This site is built from the repository's `main` branch, which can be ahead
    of PyPI. To run the exact code documented here, clone
    [rrrozhd/zeroth](https://github.com/rrrozhd/zeroth) and run `uv sync`.

Use the [Getting Started tutorial](tutorials/getting-started/index.md) for a
guided first graph, or jump to [local development](how-to/deployment/local-dev.md)
to run the API and web console.

## Primary product flow

1. **Attribute** cost and outcome to version, run, step, attempt, and subject.
2. **Debug** timelines, cohorts, and the groups that break the pipeline.
3. **Backtest** a cheaper model or workflow candidate against recorded cases.
4. **Govern** rollout with explicit economic and evidence thresholds.

`zeroth.optimization` remains the local façade over existing analytics. The
headless service API is now the primary product surface. The existing console
remains an optional open-source UI.

## Preserved platform paths

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

The [Getting Started tutorial](tutorials/getting-started/index.md) documents the
preserved graph runtime and service paths.

## Hello, Zeroth

The smallest possible smoke test — install the package, set
`OPENAI_API_KEY`, and run the script below. You should see a one-line
LLM greeting in under 5 minutes.

```python title="examples/00_hello.py"
--8<-- "00_hello.py"
```

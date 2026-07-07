# Vendored: econ_instrumentation

This package is vendored from the **Regulus** project's Python SDK, copied
in-tree so Zeroth no longer depends on the external `econ-instrumentation-sdk`
PyPI package.

- **Upstream repo:** `git@github.com:rrrozhd/regulus.git`
- **Upstream path:** `sdk/python/econ_instrumentation/`
- **Vendored at commit:** `9507aa821ec157ca2608ef520da0bf81cb9c5469`
- **SDK version at vendor time:** `0.1.1`

## Runtime dependencies

Importing `econ_instrumentation` only requires `pydantic` and `httpx` (both core
Zeroth deps). The provider integrations (`openai`, `anthropic`, `langchain`,
`langgraph`) are reflection-based and do **not** hard-import those libraries;
OpenTelemetry is a guarded lazy import in `otel.py`.

## Local changes

- `tests/` and the `regulus` console-script entry point were dropped during
  vendoring (the CLI's `demo`/`compute` subcommands reference an external
  `demo/` directory that does not exist in this repo).
- **Additive `headers_provider`** on `TelemetryTransport.__init__` and
  `InstrumentationClient.__init__` (`transport.py`, `client.py`): an optional
  `Callable[[], dict]` called once per flush to attach fresh request headers
  (Zeroth uses it to inject a short-lived service auth token). Backward
  compatible — defaults to `None` (no headers). Re-apply this after any upstream
  re-sync.

## Re-syncing from upstream

```bash
rsync -a --exclude='tests/' --exclude='__pycache__/' --exclude='*.pyc' \
  ../regulus/sdk/python/econ_instrumentation/ src/econ_instrumentation/
```

Then bump the commit hash above and re-run `uv run pytest tests/test_econ_*.py`.

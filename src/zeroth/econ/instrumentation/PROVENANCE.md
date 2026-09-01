# Provenance: zeroth.econ.instrumentation

The client-side cost-instrumentation SDK (`ExecutionEvent` schema, the
`InstrumentationClient` transport, and the reflection-based provider integrations).
Used by the Zeroth cost path (`zeroth.econ.analytics.client` / `.adapter`) to emit
execution events to the economic control plane.

## Origin

Originally the Regulus project's Python SDK (`sdk/python/econ_instrumentation/`,
version `0.1.1`). **Absorbed into Zeroth on 2026-07-10** — Zeroth now owns this code
outright. There is no upstream re-sync; edit it in place like any first-party module.

## Runtime dependencies

Importing this package only requires `pydantic` and `httpx` (both core Zeroth deps).
The provider integrations (`openai`, `anthropic`, `langchain`, `langgraph`) are
reflection-based and do **not** hard-import those libraries; OpenTelemetry is a
guarded lazy import in `otel.py`.

## Notable local behavior

- `TelemetryTransport.__init__` and `InstrumentationClient.__init__` accept an
  optional `headers_provider` (`Callable[[], dict]`, defaults to `None`) called once
  per flush to attach fresh request headers — Zeroth uses it to inject a short-lived
  service-auth token.
- `InstrumentationClient.authenticated(...)` is the additive, static-token
  onboarding path; it preserves the protected constructor signature and keeps
  the token out of model configuration and object representations. Global
  instrumentation reads `ECP_BEARER_TOKEN` when its runtime initializes.
- Executions and outcomes both expose confirmed delivery. Those methods return
  only after the plane accepts the write and raise on rejection; buffered
  delivery remains available for long-running applications.

## Lint

Excluded from Zeroth's `ruff`/`interrogate` config (`pyproject.toml`): the code
predates Zeroth's `D`/lint rules and full conformance is a deferred cleanup, not a
blocker. Remove the exclude once it's been reformatted.

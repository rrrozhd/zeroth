# 11 — Serve via the stock entrypoint (`python -m zeroth.core.service.entrypoint`)

`examples/10_serve_in_python.py` shows you how to boot a service
programmatically. This file shows the **production path** that the
Docker image uses: a persistent database, a `zeroth.yaml` config file,
environment-variable overrides, and the `uvicorn` factory pattern
from `zeroth.core.service.entrypoint`.

Everything under `examples/service/` is designed to drop straight into
your own container image or systemd unit (see the
[Sandbox container guide](../docs/how-to/deployment/sandbox-container.md)
for the isolation-focused recipe).

## Files

| File                                     | Purpose                                                                 |
| ---------------------------------------- | ----------------------------------------------------------------------- |
| `examples/service/zeroth.yaml`           | Base config. Env vars with the `ZEROTH_` prefix override these values.  |
| `examples/service/seed_deployment.py`    | One-shot: runs migrations, registers contracts, publishes + deploys the graph. |
| `examples/service/entrypoint.py`         | Extends the stock entrypoint to register the example's `AgentRunner`.  |

The stock `python -m zeroth.core.service.entrypoint` doesn't know about
your agent runners — that's user code. `examples/service/entrypoint.py`
is the canonical "drop-in extension" pattern: it mirrors the stock
`_bootstrap()` function, adds `agent_runners=...` and `auth_config=...`,
and hands the app back to uvicorn via the same factory shape.

## Run it

```bash
# 1. Seed the database (first time only).
ZEROTH_DATABASE__SQLITE_PATH=examples_service.sqlite \
    uv run python examples/service/seed_deployment.py

# 2. Start the service.
ZEROTH_DATABASE__SQLITE_PATH=examples_service.sqlite \
    ZEROTH_DEPLOYMENT_REF=examples-api \
    ZEROTH_REGULUS__ENABLED=false \
    ZEROTH_WEBHOOK__ENABLED=false \
    ZEROTH_APPROVAL_SLA__ENABLED=false \
    ZEROTH_REDIS__MODE=disabled \
    OPENAI_API_KEY=sk-... \
    uv run python examples/service/entrypoint.py
```

The service binds to `127.0.0.1:8000` by default. Override with
`HOST=0.0.0.0` and `PORT=...`.

## Use it

```bash
# Health check (no auth required — load balancers probe this).
curl http://127.0.0.1:8000/health

# Submit a run.
curl -X POST http://127.0.0.1:8000/v1/runs \
    -H "X-API-Key: demo-operator-key" \
    -H "Content-Type: application/json" \
    -d '{"input_payload": {"question": "What is Zeroth?"}}'

# Poll the run. POST /v1/runs returns immediately with status `queued`;
# the durable worker picks it up within a few hundred milliseconds.
curl -H "X-API-Key: demo-operator-key" \
    http://127.0.0.1:8000/v1/runs/<run_id>
```

## What's different from `10_serve_in_python.py`?

| Concern                 | `10_serve_in_python.py`               | Stock entrypoint path                                     |
| ----------------------- | ------------------------------------- | --------------------------------------------------------- |
| Database                | Temp SQLite, recreated on every run   | Persistent SQLite or Postgres — survives restarts         |
| Deployment seeding      | Inline, before every launch           | Separate one-shot (`seed_deployment.py`)                  |
| Settings source         | Python kwargs                         | `zeroth.yaml` + `ZEROTH_*` env vars (`get_settings()`)   |
| Bootstrap factory       | Direct `bootstrap_service(...)` call  | `app_factory()` — async-to-sync for uvicorn               |
| Intended home           | Dev scratch                           | Docker image                                              |

## Real dependencies

When you enable Regulus, webhooks, or the durable dispatcher, they
require real backends:

| Subsystem        | Env var                                   | Backend                             |
| ---------------- | ----------------------------------------- | ----------------------------------- |
| Cost tracking    | `ZEROTH_REGULUS__ENABLED=true`            | Regulus service over HTTP           |
| Webhooks         | `ZEROTH_WEBHOOK__ENABLED=true`            | Postgres rows + background worker  |
| Approval SLA     | `ZEROTH_APPROVAL_SLA__ENABLED=true`       | Postgres rows + background worker  |
| Memory (Redis)   | `ZEROTH_REDIS__MODE=local|production`     | Redis                               |
| Dispatch (ARQ)   | `ZEROTH_DISPATCH__ARQ_ENABLED=true`       | Redis + ARQ workers                 |
| Sandbox sidecar  | `ZEROTH_SANDBOX__BACKEND=sidecar`         | `sandbox-sidecar` service           |

The example defaults all of these off so the file runs on a laptop.

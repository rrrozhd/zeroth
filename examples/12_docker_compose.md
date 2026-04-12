# 12 — Docker Compose topology

The repo ships a `docker-compose.yml` at the root that stands up the
full production-shaped stack around the Zeroth service. This doc walks
through the pieces so the stack isn't a black box the first time you
`docker compose up`.

## Services

```text
 ┌─────────────┐    HTTP     ┌──────────────────┐
 │   client    │────────────▶│  zeroth (api)    │
 └─────────────┘             │  uvicorn :8000   │
                             └──────┬────────┬──┘
                                    │        │
                              Postgres    Redis
                                    │        │
                             ┌──────▼──┐ ┌───▼─────┐
                             │ postgres │ │  redis  │
                             └──────────┘ └─────────┘
                                    ▲        ▲
                                    └────┬───┘
                                         │
                            ┌────────────┴────────────┐
                            │    optional services    │
                            └────────────┬────────────┘
                                         │
                        ┌────────────────┼────────────────┐
                        ▼                ▼                ▼
                ┌────────────┐   ┌──────────────┐  ┌──────────────┐
                │  regulus   │   │ sandbox-     │  │  (your own)  │
                │  (cost)    │   │ sidecar      │  │  workers     │
                └────────────┘   └──────────────┘  └──────────────┘
```

| Service            | What it does                                                                                       | Gate                                     |
| ------------------ | -------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `zeroth`           | The FastAPI app — runs `python -m zeroth.core.service.entrypoint`. Binds to `:8000` inside.        | Always on.                               |
| `postgres`         | Graphs, runs, approvals, audit trail, deployments, webhooks, dead-letters.                         | `ZEROTH_DATABASE__BACKEND=postgres`      |
| `redis`            | Rate-limit token buckets, memory connectors, ARQ dispatch, SLA metadata.                          | `ZEROTH_REDIS__MODE=production`          |
| `regulus`          | Budget caps and cost dashboards.                                                                   | `ZEROTH_REGULUS__ENABLED=true`           |
| `sandbox-sidecar`  | Runs executable units in isolated containers instead of local subprocesses.                        | `ZEROTH_SANDBOX__BACKEND=sidecar`        |

The stock `docker-compose.yml` only turns on what you need — treat each
`ZEROTH_*` env var as a capability switch.

## Bringing your own graph

The `zeroth` service in `docker-compose.yml` runs the **stock**
entrypoint. It loads a deployment by ref and starts the API, but it
does **not** know about your agent runners. There are two options:

1. **Extend the entrypoint** (recommended — same pattern as
   `examples/service/entrypoint.py`). Change the compose `command:`
   to `python -m examples.service.entrypoint`, bake your extension into
   the image via `Dockerfile`, and you're done.
2. **Use admin APIs** to register agents at runtime via
   `/v1/admin/*` — useful for multi-tenant deployments where the
   graph set isn't known at image-build time.

## Minimum viable override

A tiny `docker-compose.override.yml` for local experiments:

```yaml
services:
  zeroth:
    build:
      context: .
      dockerfile: Dockerfile
    command: ["python", "-m", "examples.service.entrypoint"]
    environment:
      ZEROTH_DATABASE__BACKEND: postgres
      ZEROTH_DATABASE__POSTGRES_DSN: postgresql://zeroth:zeroth@postgres/zeroth
      ZEROTH_REDIS__MODE: production
      ZEROTH_REDIS__HOST: redis
      ZEROTH_DEPLOYMENT_REF: examples-api
      OPENAI_API_KEY: ${OPENAI_API_KEY}
    ports: ["8000:8000"]
```

```bash
docker compose up postgres redis    # start the backends
docker compose run --rm zeroth python examples/service/seed_deployment.py
docker compose up zeroth
```

## Where to look next

* `Dockerfile` — how the image is built.
* `zeroth.yaml` — the base settings file.
* `src/zeroth/core/config/settings.py` — every `ZEROTH_*` env var the
  service understands.
* `src/zeroth/core/service/entrypoint.py` — the stock entrypoint you're
  extending in `examples/service/entrypoint.py`.

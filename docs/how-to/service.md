# Running the service

## Overview

To deploy a Zeroth graph you run exactly one process: a `uvicorn` worker
that imports `zeroth.core.service.entrypoint` and calls its `app_factory`.
Everything else — Alembic migrations, identity, storage, dispatch, the
orchestrator, webhooks, econ — is wired for you by `bootstrap_service`.
This page shows the production entrypoint, how to mount under a prefix,
and the exact startup ordering you must respect.

## Minimal example

The shipped production entrypoint is `src/zeroth/core/service/entrypoint.py`.
In a container you run:

```bash
uvicorn zeroth.core.service.entrypoint:app_factory \
    --factory \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers
```

or equivalently just invoke the module's `main()`, which also runs
Alembic migrations when the Postgres backend is configured:

```bash
python -m zeroth.core.service.entrypoint
```

For tests and scripts you can skip uvicorn entirely and build an app
in-process:

```python
import asyncio
from zeroth.core.service import bootstrap_service, create_app
from zeroth.core.storage.factory import create_database
from zeroth.core.config.settings import get_settings

async def make_app():
    settings = get_settings()
    db = await create_database(settings)
    boot = await bootstrap_service(db, deployment_ref="default")
    return create_app(boot)

app = asyncio.run(make_app())
```

## Common patterns

- **Mounting under a prefix** — Put Zeroth behind a reverse proxy and
  strip the prefix, or mount the returned `FastAPI` app as a sub-app of
  an outer router. Do *not* hand-edit route prefixes inside `create_app`.
- **Healthchecks** — `GET /health` returns deployment ref, deployment
  version, and graph version ref. Wire it into your orchestrator's
  liveness + readiness probes.
- **Multiple deployments, one image** — Set `ZEROTH_DEPLOYMENT_REF` to
  select which deployment the process serves. One image, N services.
- **TLS termination** — Pass `ssl_keyfile` / `ssl_certfile` via
  settings when you want uvicorn to terminate TLS directly.

## Pitfalls

1. **Startup ordering** — Bootstrap builds components in a specific
   order: settings → identity → storage → dispatch worker → orchestrator
   → service routers → webhook delivery worker. Breaking that order (e.g.
   constructing the worker before storage) deadlocks the lifespan hook.
2. **Skipping migrations** — Running `app_factory` directly (bypassing
   `main()`) does *not* run Alembic. In production always go through
   `python -m zeroth.core.service.entrypoint`.
3. **Mutating the app after lifespan starts** — The lifespan context
   starts background workers; adding routes after that is a race.
4. **Missing `ZEROTH_DEPLOYMENT_REF`** — Defaults to `"default"`, which
   is usually not what you want in production.
5. **Proxy headers disabled** — Behind a load balancer, forgetting
   `--proxy-headers` breaks identity propagation and audit correlation.

## Reference cross-link

API reference will live under the Reference quadrant (Phase 32).
Related: [concepts/service](../concepts/service.md) ·
[dispatch how-to](dispatch.md) · [secrets how-to](secrets.md).

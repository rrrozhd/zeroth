# Embedded in a host application

Embedded mode skips the HTTP surface entirely. You import `zeroth.core` into
your own FastAPI app, CLI, worker, or notebook and drive the orchestrator
directly. Use it when you already have a deployable process and just want
`zeroth-core` as an in-process library.

## Use case

- You already run a FastAPI service and want to expose graph runs on your
  own routes
- A CLI tool that invokes graphs on demand
- A background worker that runs a graph per job
- Notebook or scripting usage where an extra HTTP hop is overhead

## Prerequisites

- Python 3.12+
- `zeroth-core` installed in the host project's virtualenv
- An async context to call the orchestrator from

## Install

```bash
pip install zeroth-core
# Or, with optional backends matching your host app
pip install "zeroth-core[memory-pg]"
```

## Minimal pattern

```python
import asyncio

from zeroth.core.config.settings import get_settings
from zeroth.core.service.bootstrap import bootstrap_service
from zeroth.core.storage.factory import create_database


async def main() -> None:
    # Settings are read from ZEROTH_* env vars (and any .env file present).
    settings = get_settings()
    database = await create_database(settings)
    bootstrap = await bootstrap_service(database, deployment_ref="default")

    orchestrator = bootstrap.orchestrator
    # `bootstrap` exposes every wired subsystem — graphs, runs, audit, etc.
    # Submit runs via the orchestrator or via the runs service.
    runs = bootstrap.runs
    run = await runs.start_run(
        graph_ref="my-graph@v1",
        inputs={"query": "hello"},
    )
    print(run.run_id)


if __name__ == "__main__":
    asyncio.run(main())
```

`bootstrap_service` is the same wiring function used by the HTTP entrypoint,
so every subsystem (orchestrator, runs, audit, memory, guardrails, secrets)
is fully initialized. See the
[Python API Reference — service](../../reference/python-api/service.md) for
the full `Bootstrap` dataclass surface.

## FastAPI host app

If your host app wants to expose the `zeroth-core` HTTP routes alongside its
own, mount the service sub-app:

```python
from fastapi import FastAPI

from zeroth.core.service.app import create_app
from zeroth.core.service.bootstrap import bootstrap_service
from zeroth.core.storage.factory import create_database
from zeroth.core.config.settings import get_settings


async def lifespan(app: FastAPI):
    settings = get_settings()
    db = await create_database(settings)
    bootstrap = await bootstrap_service(db, deployment_ref="default")
    app.state.zeroth = create_app(bootstrap)
    yield


host = FastAPI(lifespan=lifespan)
```

You can then mount `app.state.zeroth` under a prefix (`host.mount("/zeroth",
app.state.zeroth)`) or import individual routers from
`zeroth.core.service.run_api`, `approval_api`, etc.

## Storage and migrations

- **SQLite:** no migration step required; the schema is created lazily on
  first boot.
- **Postgres:** run `alembic upgrade head` from the host project before the
  first boot. Reuse the `zeroth.core.storage.alembic` package as the script
  location.

## Common gotchas

- **Event loops:** `bootstrap_service` is async. Call it from inside an
  existing loop — do not wrap it in `asyncio.run()` repeatedly from
  synchronous code, or you will churn the database pool.
- **Double bootstrap:** build the `Bootstrap` once per process and share it.
  Each bootstrap opens its own DB connections.
- **Logging:** `zeroth-core` uses the stdlib `logging` module. Configure the
  root logger in your host app; do not fight it with a second handler set.
- **Settings precedence:** env vars win over `.env`. If the host app already
  loads `.env` via `python-dotenv`, make sure `ZEROTH_*` vars are loaded
  before `get_settings()` is called.

## Related references

- [Python API Reference — orchestrator](../../reference/python-api/orchestrator.md)
- [Python API Reference — runs](../../reference/python-api/runs.md)
- [Configuration Reference](../../reference/configuration.md)

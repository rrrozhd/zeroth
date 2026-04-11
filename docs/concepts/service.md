# Service

## What it is

The `zeroth.core.service` subsystem is the **deployment-bound FastAPI
wrapper** that turns a Zeroth graph into a standalone HTTP API. It
consists of three tightly cooperating modules: `entrypoint.py` (the
process-level main), `bootstrap.py` (dependency wiring), and `app.py`
(the FastAPI application factory with all routers mounted).

## Why it exists

Zeroth graphs are not libraries — they are deployed products. Every graph
gets its own service process with its own auth, its own run store, its
own webhook workers, and its own health endpoint. `service` is the thin
but opinionated layer that assembles those components in the correct
order, wires them into FastAPI, and exposes a single importable app
factory that `uvicorn` can run.

## Where it fits

`service` sits at the top of the dependency graph: it pulls in
[identity](identity.md) (authentication), [storage](storage.md) (run
repository), [dispatch](dispatch.md) (run worker), the
[orchestrator](orchestrator.md), [webhooks](webhooks.md) delivery
workers, and the [econ](econ.md) instrumented adapter. Everything else
in Zeroth is a module; `service` is the glue that becomes a running
process.

## Key types

- **`create_app(bootstrap) -> FastAPI`** — Application factory. Given a
  `ServiceBootstrap`, returns a fully-wired FastAPI app with lifespan
  hooks for the run worker, webhook delivery worker, queue gauges, and
  approval SLA checker.
- **`ServiceBootstrap`** — Dataclass containing every wired-up
  dependency: `graph`, `orchestrator`, `run_repository`, `worker`,
  `authenticator`, `webhook_service`, and friends.
- **`bootstrap_service(database, deployment_ref) -> ServiceBootstrap`** —
  Async builder that reads settings, resolves the deployment, constructs
  each subsystem in dependency order, and returns the bootstrap object.
- **`DeploymentBootstrapError`** — Raised when bootstrap cannot resolve
  the named deployment or its graph version.
- **`entrypoint.app_factory()`** — The uvicorn-callable factory used in
  production containers: runs Alembic migrations, bootstraps, returns an
  app.

## See also

- Usage Guide: [how-to/service](../how-to/service.md)
- Related: [identity](identity.md), [orchestrator](orchestrator.md),
  [dispatch](dispatch.md)

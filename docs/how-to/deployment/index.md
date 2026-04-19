# Deployment

`zeroth-core` is a Python library. You bring the container, systemd unit, or
host process. The pages below document the deployment shapes we support and
the hooks the library expects.

## Mode comparison

| Mode | When to use | Command | Prerequisites |
|---|---|---|---|
| [Local development](local-dev.md) | Hacking on graphs, running tutorials, exercising examples | `uv run zeroth-core serve` | Python 3.12, `uv` |
| [Standalone service](standalone-service.md) | Production single-node deploy fronted by nginx/Caddy | `uvicorn zeroth.core.service.entrypoint:app_factory --factory` | Python 3.12, Postgres, TLS cert |
| [Embedded library](embedded-library.md) | Importing `zeroth.core` in a host FastAPI/CLI/worker | `from zeroth.core.service.bootstrap import bootstrap_service` | `zeroth-core` in the host venv |
| [Sandbox container](sandbox-container.md) | Running untrusted executable units in an isolated container | Your own `docker run` | `DockerSandboxSettings` configured |
| [With Regulus](with-regulus.md) | Budget enforcement and cost economics | Your own compose/manifest with a Regulus service | Running Regulus container |

## Picking a mode

- If you are exploring the runtime, start with **local-dev**. It runs against
  SQLite and needs no external services.
- For a real single-node deployment, use **standalone-service** with a
  reverse proxy and systemd unit.
- If you already have a FastAPI app, a CLI, or a worker process, **embedded
  library** lets you skip the HTTP surface entirely and drive the orchestrator
  directly from Python.
- If your graphs run untrusted executable units, follow **sandbox-container**
  to wire up an isolated Docker/Podman sidecar.
- Any of the above can be extended with **with-regulus** to add cost-budget
  enforcement via the `econ-instrumentation-sdk`.

## Cross-cutting references

- [Configuration Reference](../../reference/configuration.md) — every
  `ZEROTH_*` env var, default, and secret flag.
- [HTTP API Reference](../../reference/http-api.md) — the REST surface exposed
  by the service layer.
- [Python API Reference](../../reference/python-api.md) — the importable
  surface for embedded use.

Regardless of the mode you pick, the service honors the same settings
namespace (`ZEROTH_*`), so env files and secret material are portable across
modes.

# Local development

Local development mode runs `zeroth-core` as a single process against SQLite,
with no external services required. Use it for hacking on graphs, running
tutorials, or exercising the examples shipped with the repository.

## Use case

- Exploring the runtime for the first time
- Running the [Getting Started tutorial](../../tutorials/getting-started.md)
- Iterating on graph authoring without spinning up Postgres or Redis
- Smoke-testing changes against the local checkout before pushing

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) installed
- (Optional) LLM API keys exported in the shell or a `.env` file

## Install

Pick either a PyPI install or a checkout of the repository.

```bash
# Option A — from PyPI
pip install zeroth-core

# Option A (with extras)
pip install "zeroth-core[memory-pg,dispatch]"

# Option B — from a local checkout
git clone https://github.com/rrrozhd/zeroth.git
cd zeroth
uv sync
```

## Run

```bash
# From a pip install
zeroth-core serve

# From a uv-managed checkout
uv run zeroth-core serve
```

The service binds `0.0.0.0:8000` by default and stores state in a local
SQLite database (`./zeroth.db`). On first boot it creates the schema
automatically — no Alembic migration step is required for SQLite.

## Verify

```bash
curl -f http://localhost:8000/healthz
# -> {"status":"ok"}
```

Open the interactive API explorer at `http://localhost:8000/docs` to poke at
the REST surface. See the
[HTTP API Reference](../../reference/http-api.md) for the full contract.

## Default storage

Local dev uses the SQLite backend. Override with env vars:

```bash
export ZEROTH_DATABASE__BACKEND=sqlite
export ZEROTH_DATABASE__SQLITE_PATH=./zeroth.db
```

See [Configuration Reference — database](../../reference/configuration.md)
for every knob the `database` section exposes.

## Common gotchas

- **Port 8000 in use:** set `ZEROTH_SERVICE__PORT=8001` (or another free port)
  before launching. The `PORT` env var is also honored.
- **Missing LLM keys:** agent nodes fail fast if their provider key is
  missing. Put `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. in a `.env` file
  next to your working directory.
- **Stale SQLite file:** delete `./zeroth.db` to reset local state between
  tutorials. No migration rollback is needed.
- **Python version:** the runtime requires 3.12. `uv python install 3.12`
  if your system default is older.

## Next steps

- Graduate to a realistic stack with [Docker Compose](docker-compose.md).
- Embed the runtime in a host app with
  [Embedded library](embedded-library.md).

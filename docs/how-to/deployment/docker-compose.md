# Docker Compose

Docker Compose mode runs `zeroth-core` against a realistic backing stack —
Postgres, Redis, an optional Regulus companion, and a sandbox sidecar — all
wired together by the `docker-compose.yml` shipped at the repository root.

## Use case

- Full-stack local development that mirrors production
- Integration testing against real Postgres and Redis
- Demoing the platform without provisioning cloud infrastructure
- Running the sandbox sidecar for executable-unit isolation

## Prerequisites

- Docker 24+
- Docker Compose v2 (`docker compose`, not the legacy `docker-compose`)
- ~4 GB of free memory for the full stack

## Get the compose file

Either clone the repository or copy
[`docker-compose.yml`](https://github.com/rrrozhd/zeroth/blob/main/docker-compose.yml)
into an empty directory.

```bash
git clone https://github.com/rrrozhd/zeroth.git
cd zeroth
```

The bundled file wires five services:

- `zeroth` — the `zeroth-core` API (built from the repo `Dockerfile`)
- `postgres` — Postgres 16 with a persistent `pgdata` volume
- `redis` — Redis 7 for arq dispatch and ephemeral state
- `regulus` — the econ companion (see [with-regulus](with-regulus.md))
- `sandbox-sidecar` — the sandboxed executable-unit runner

The core service is pre-wired with:

```yaml
environment:
  ZEROTH_DATABASE__BACKEND: postgres
  ZEROTH_DATABASE__POSTGRES_DSN: "postgresql://zeroth:zeroth@postgres:5432/zeroth"
  ZEROTH_REDIS__HOST: redis
  ZEROTH_DISPATCH__ARQ_ENABLED: "true"
  ZEROTH_REGULUS__ENABLED: "true"
  ZEROTH_REGULUS__BASE_URL: "http://regulus:8080/v1"
  ZEROTH_SANDBOX__BACKEND: sidecar
  ZEROTH_SANDBOX__SIDECAR_URL: "http://sandbox-sidecar:8001"
```

## Start the stack

```bash
docker compose up -d
docker compose logs -f zeroth
```

The `zeroth` service runs Alembic migrations against Postgres as part of its
production entrypoint, so the schema is ready before the API accepts traffic.

## Verify

```bash
docker compose ps
curl -f http://localhost:8000/healthz
```

You can override any setting by adding a `.env` file next to the compose file
— every `ZEROTH_*` var documented in
[Configuration Reference](../../reference/configuration.md) is honored.

## Teardown

```bash
docker compose down         # stop services, keep volumes
docker compose down -v      # stop services and drop Postgres data
```

## Common gotchas

- **Migrations:** the container entrypoint runs `alembic upgrade head`
  automatically for the Postgres backend. If you bypass the entrypoint, run
  `docker compose exec zeroth uv run alembic upgrade head` manually.
- **Stale image:** after pulling new code, rebuild with
  `docker compose build --no-cache zeroth`.
- **Port conflicts:** expose a different host port by editing the `ports:`
  block on the `zeroth` service.
- **Sandbox sidecar image:** `zeroth-sandbox-sidecar:latest` must be built or
  pulled locally before `docker compose up`.

## Next steps

- Add cost-budget enforcement with [With Regulus](with-regulus.md).
- Graduate to a production single-node deploy with
  [Standalone service](standalone-service.md).

# Provenance: zeroth.econ.plane (economic control plane)

The economic control-plane backend — cost attribution, budget caps, and the
dashboard KPI source. A self-contained FastAPI subsystem (its own DB, auth, and
migrations) mounted in-process at `/regulus` when `ZEROTH_REGULUS__ENABLED=true`.

## Origin

Originally the Regulus backend (`backend/src/econ_plane/` + `backend/alembic/`,
version `0.1.0`). **Absorbed into Zeroth on 2026-07-10** — Zeroth now owns this code
outright. There is no upstream re-sync; edit it in place like any first-party module.

## Dependencies

Runtime deps to import + boot live in the `regulus` optional-extra
(`pyproject.toml`): `email-validator`, `numpy`,
`dramatiq`. fastapi/uvicorn/httpx/sqlalchemy/pydantic-settings/redis are already core.
Install with `uv sync --extra regulus` (or `--all-extras`). Optional / not needed to
boot: `psycopg` (Postgres only), `kafka-python` (lazy), `prometheus-client` (lazy),
`opentelemetry-*` (gated by `ECP_OTEL_METRICS_ENABLED`, default off).

## Configuration & isolation

- Settings use the **`ECP_`** env prefix (no collision with Zeroth's `ZEROTH_`).
  Kept as-is deliberately — renaming is a config-breaking change to every deployment.
- Its own database (`ECP_DATABASE_URL`, default `sqlite+pysqlite:///./econ_plane.db`),
  schema created at startup via `bootstrap()` (`Base.metadata.create_all` + role
  seeding); no migration step required for SQLite. `_migrations/` (Alembic) is for
  offline Postgres ops only and is never invoked at runtime.
- Its own JWT bearer auth (`zeroth.econ.plane.auth`). Tokens are minted via the open
  `POST /v1/auth/token` issuer signed with `ECP_JWT_SECRET` (set this in prod).

The Zeroth ↔ control-plane wire stays HTTP. Budget admission now fails closed if
the backend is unreachable, malformed, or reports incomplete measurement;
explicit fail-open remains development/availability compatibility only.

## Running standalone

```bash
uv run uvicorn zeroth.econ.plane.main:app --port 8000
```

## Lint

Excluded from Zeroth's `ruff`/`interrogate` config (`pyproject.toml`): the ~4600 LOC
predate Zeroth's `D`/lint rules and full conformance is a deferred cleanup, not a
blocker. Remove the exclude once it's been reformatted.

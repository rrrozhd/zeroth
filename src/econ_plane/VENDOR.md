# Vendored: econ_plane (Regulus economic control plane)

The Regulus backend, brought in-tree so the economic control plane (cost
attribution, budget caps, the dashboard KPI source) is part of the Zeroth repo
and deployable rather than a separately-operated standalone service.

- **Upstream repo:** `git@github.com:rrrozhd/regulus.git`
- **Upstream path:** `backend/src/econ_plane/` (+ `backend/alembic/` → `_migrations/`)
- **Vendored at commit:** `9507aa821ec157ca2608ef520da0bf81cb9c5469`
- **Version at vendor time:** `0.1.0` (`econ-plane-backend`)

## Dependencies

Runtime deps to import + boot live in the `regulus` optional-extra
(`pyproject.toml`): `python-jose[cryptography]`, `email-validator`, `numpy`,
`dramatiq`. fastapi/uvicorn/httpx/sqlalchemy/pydantic-settings/redis are already
core Zeroth deps. Install with:

```bash
uv sync --extra regulus     # or: uv sync --all-extras
```

Optional / not needed to boot: `psycopg` (only for Postgres; the default
`ECP_DATABASE_URL` is SQLite), `kafka-python` (lazy, connector workers),
`prometheus-client` (lazy, `/metrics`), `opentelemetry-*` (gated by
`ECP_OTEL_METRICS_ENABLED`, default off).

## Configuration & isolation

- Settings use the **`ECP_`** env prefix (no collision with Zeroth's `ZEROTH_`).
- Its own database (`ECP_DATABASE_URL`, default `sqlite+pysqlite:///./econ_plane.db`),
  schema created at startup via `bootstrap()` (`Base.metadata.create_all` + role
  seeding); no migration step required for SQLite. `_migrations/` (Alembic) is
  for offline Postgres ops only and is never invoked at runtime.
- Its own JWT bearer auth (`econ_plane.auth`). Tokens are minted via the open
  `POST /v1/auth/token` issuer signed with `ECP_JWT_SECRET` (set this in prod).

The Zeroth ↔ econ_plane wire stays HTTP (per design decision D-12, budget
enforcement fails open if the backend is unreachable). The Zeroth-side econ
client/budget/cost code is unchanged.

## Running standalone

```bash
uv run uvicorn econ_plane.main:app --port 8000
```

## Re-syncing from upstream

```bash
rsync -a --exclude='__pycache__/' --exclude='*.pyc' --exclude='tests/' \
  ../regulus/backend/src/econ_plane/ src/econ_plane/
rsync -a --exclude='__pycache__/' \
  ../regulus/backend/alembic/ src/econ_plane/_migrations/
```

Then bump the commit hash above.

## Out of scope (not vendored)

The Regulus Vite dashboard (superseded by Zeroth's in-repo `frontend/` console),
`demo/`, `templates/`, and the backend's own `tests/` (kept out of `src/` to
avoid polluting Zeroth's pytest collection; run them from the upstream repo).

# With the Regulus economic control plane

Regulus is the economics control plane that tracks LLM spend and exposes the
cost/KPI data behind budget caps. It is **part of the `zeroth-core` package** —
absorbed in-repo, Zeroth-owned, not a separate project:

- the instrumentation SDK lives at `src/zeroth/econ/instrumentation`
  (import `zeroth.econ.instrumentation`); and
- the backend lives at `src/zeroth/econ/plane` (the `zeroth.econ.plane` FastAPI
  app), installable via the `regulus` optional extra.

You can run it **in-process** (mounted inside the Zeroth app — one service) or
as a **separate process** (the same app, started on its own port). Both use the
same in-repo source.

## Use case

- Tracking LLM spend per node / run / tenant
- Feeding the cost dashboard (`/v1/.../cost`) and the console cost page
- Budget-cap checks at fan-out (fail-closed by default — see below)

## Install

The instrumentation SDK is always available (part of the package, core deps
only). To run the backend, install the `regulus` extra:

```bash
uv sync --extra regulus        # or: uv sync --all-extras
# pip equivalent:
pip install "zeroth-core[regulus]"
```

This pulls the backend's extra runtime deps (`python-jose`, `email-validator`,
`numpy`, `dramatiq`); fastapi/uvicorn/httpx/sqlalchemy/redis are already core.
The backend defaults to SQLite; add `psycopg` (see the `memory-pg` extra) for
Postgres.

## Configure

Zeroth talks to Regulus over HTTP, controlled by the `regulus` settings section
(prefix `ZEROTH_REGULUS__`):

```bash
ZEROTH_REGULUS__ENABLED=true
ZEROTH_REGULUS__BASE_URL=http://127.0.0.1:8000/regulus/v1   # in-process mount
```

The bundled backend has its **own** settings (prefix `ECP_`), database, and JWT
auth — independent of Zeroth's. Set at least:

```bash
ECP_DATABASE_URL=sqlite+pysqlite:////var/lib/zeroth/econ_plane.db
ECP_JWT_SECRET=<a-strong-secret>
ECP_SERVICE_PRINCIPAL_TENANT_ID=<the-tenant-served-by-this-instance>
# Optional workspace partition and identity metadata:
ECP_SERVICE_PRINCIPAL_WORKSPACE_ID=<workspace-id>
ECP_SERVICE_PRINCIPAL_SUBJECT=zeroth-service
ECP_SERVICE_PRINCIPAL_EMAIL=zeroth-service@example.com
ECP_SERVICE_PRINCIPAL_ROLES=Admin
```

## Topology A — in-process mount (recommended)

When `ZEROTH_REGULUS__ENABLED=true`, the Zeroth app mounts the bundled backend
at `/regulus` (its schema is initialized from Zeroth's lifespan). The mount sits
behind Zeroth's API-key gate, and econ_plane enforces its own JWT on top; Zeroth's
self-calls authenticate automatically (see "Self-auth" below). One process serves
both. Point the base URL at the mount:

```bash
ZEROTH_REGULUS__ENABLED=true
ZEROTH_REGULUS__BASE_URL=http://127.0.0.1:8000/regulus/v1
```

If the `regulus` extra is not installed, the mount is skipped with a warning and
Zeroth still boots, but budget admission fails closed by default while the
economics backend is unavailable; telemetry delivery remains best effort.

## Topology B — separate process

> Note: whenever `ZEROTH_REGULUS__ENABLED=true` and the `regulus` extra is
> installed, Zeroth *also* mounts an in-process copy at `/regulus` regardless of
> where `BASE_URL` points. In a separate-process deployment that mount is simply
> unused — point `BASE_URL` at the standalone backend below.

Run the bundled backend on its own port and point Zeroth at it:

```bash
uv run uvicorn zeroth.econ.plane.main:app --port 8000   # the Regulus backend
ZEROTH_REGULUS__BASE_URL=http://regulus:8000/v1     # in Zeroth's env
```

```yaml
services:
  zeroth:
    environment:
      ZEROTH_REGULUS__ENABLED: "true"
      ZEROTH_REGULUS__BASE_URL: "http://regulus:8000/v1"
    depends_on: [regulus]
  regulus:
    image: zeroth-core:latest          # same image; runs zeroth.econ.plane.main:app
    command: uvicorn zeroth.econ.plane.main:app --host 0.0.0.0 --port 8000
    environment:
      ECP_JWT_SECRET: "${ECP_JWT_SECRET}"
      ECP_SERVICE_PRINCIPAL_TENANT_ID: "${TENANT_ID}"
```

## Verify

```bash
# in-process:
curl -s http://127.0.0.1:8000/regulus/health        # -> {"status":"ok"}
# separate process:
curl -s http://localhost:8000/health                 # -> {"status":"ok"}
```

## Behavior & gotchas

- **Budget admission is fail-closed by default.** If Regulus is unreachable,
  rejects the request, or returns malformed/incomplete measurements, the run is
  denied. `ZEROTH_REGULUS__FAIL_CLOSED=false` explicitly chooses legacy
  fail-open compatibility; do not use it where caps are required. Cost-event
  delivery and dashboard reads may still degrade independently.
- **Self-auth (how cost events flow).** econ_plane protects its ingest and KPI
  endpoints with its own JWT, and the in-process mount also sits behind Zeroth's
  API-key gate. Zeroth's self-calls (SDK ingest, budget, cost) carry **both**: a
  freshly minted econ_plane Admin JWT (signed with `ECP_JWT_SECRET`, short TTL)
  and Zeroth's own first service `X-API-Key`. So cost events persist as long as
  `ECP_JWT_SECRET` is set and at least one Zeroth service key
  (`ZEROTH_SERVICE_API_KEYS_JSON`) is configured. With no Zeroth service key the
  self-calls carry only the Bearer; in the gated in-process topology that yields
  `401`; events drop and budget admission denies by default, so configure a
  service key for in-process use.
  (`ZEROTH_REGULUS__API_KEY` remains unused — the bundled flow does not need it.)
- **Trusted tenant claims.** The internal JWT takes its subject, roles,
  tenant, and optional workspace only from the `ECP_SERVICE_PRINCIPAL_*`
  settings above. Protected persistence is bound to those claims. A body,
  query parameter, or event-metadata tenant must match; it cannot select a
  different tenant.
- **Standalone token issuer.** `POST /v1/auth/token` is absent by default. For
  deliberately insecure local compatibility only,
  `ECP_INSECURE_PUBLIC_TOKEN_ISSUER_ENABLED=true` exposes the legacy issuer.
  Never enable it in a shared or production environment. It remains restricted
  to subjects already provisioned in the asserted tenant; request-selected
  roles are accepted only because the flag explicitly opts into the legacy
  development behavior.
- **Resource scope.** Operational data is tenant-scoped. The only global shared
  references are roles, user-role links, pricing catalog, and tool-pricing
  catalog. Authorization (G02) grants an operation; structural tenancy (G04)
  limits the rows that operation can reach.
- **Reaching `/regulus` externally** requires Zeroth's `X-API-Key` (no bypass);
  econ's open token issuer is therefore *not* internet-exposed by enabling the
  mount.
- **Settings isolation.** Zeroth uses the `ZEROTH_` prefix; the bundled backend
  uses `ECP_`. They do not collide.
- **Migrations.** Startup compatibility handles SQLite. Managed deployments
  should run `uv run alembic -c alembic-econ.ini upgrade head`; the dedicated
  config targets the chain under `src/zeroth/econ/plane/_migrations`. Revision
  `20260830_11` adds and backfills the economic-debugger identity spine;
  `20260830_12` adds immutable workflow-version outcome definitions; and
  `20260830_13` adds tenant-bound provider bills and cost buckets.

## Related references

- [Economics concept page](../../concepts/econ.md)
- [Configuration Reference](../../reference/configuration.md)
- Provenance: `src/zeroth/econ/instrumentation/PROVENANCE.md`,
  `src/zeroth/econ/plane/PROVENANCE.md`

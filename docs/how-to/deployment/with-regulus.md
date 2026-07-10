# With the Regulus economic control plane

Regulus is the economics control plane that tracks LLM spend and exposes the
cost/KPI data behind budget caps. It is **part of the `zeroth-core` package** —
absorbed in-repo, Zeroth-owned, not a separate project:

- the instrumentation SDK lives at `src/zeroth/core/econ/instrumentation`
  (import `zeroth.core.econ.instrumentation`); and
- the backend lives at `src/zeroth/econ_plane` (the `zeroth.econ_plane` FastAPI
  app), installable via the `regulus` optional extra.

You can run it **in-process** (mounted inside the Zeroth app — one service) or
as a **separate process** (the same app, started on its own port). Both use the
same in-repo source.

## Use case

- Tracking LLM spend per node / run / tenant
- Feeding the cost dashboard (`/v1/.../cost`) and the console cost page
- Budget-cap checks at fan-out (fail-open by design — see below)

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
Zeroth still boots (econ simply stays disabled / fail-open).

## Topology B — separate process

> Note: whenever `ZEROTH_REGULUS__ENABLED=true` and the `regulus` extra is
> installed, Zeroth *also* mounts an in-process copy at `/regulus` regardless of
> where `BASE_URL` points. In a separate-process deployment that mount is simply
> unused — point `BASE_URL` at the standalone backend below.

Run the bundled backend on its own port and point Zeroth at it:

```bash
uv run uvicorn zeroth.econ_plane.main:app --port 8000   # the Regulus backend
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
    image: zeroth-core:latest          # same image; runs zeroth.econ_plane.main:app
    command: uvicorn zeroth.econ_plane.main:app --host 0.0.0.0 --port 8000
    environment:
      ECP_JWT_SECRET: "${ECP_JWT_SECRET}"
```

## Verify

```bash
# in-process:
curl -s http://127.0.0.1:8000/regulus/health        # -> {"status":"ok"}
# separate process:
curl -s http://localhost:8000/health                 # -> {"status":"ok"}
```

## Behavior & gotchas

- **Fail-open (decision D-12).** If Regulus is unreachable *or rejects the
  request*, budget enforcement **allows** the run and cost reads return
  unavailable — Regulus never blocks execution. This is deliberate.
- **Self-auth (how cost events flow).** econ_plane protects its ingest and KPI
  endpoints with its own JWT, and the in-process mount also sits behind Zeroth's
  API-key gate. Zeroth's self-calls (SDK ingest, budget, cost) carry **both**: a
  freshly minted econ_plane Admin JWT (signed with `ECP_JWT_SECRET`, short TTL)
  and Zeroth's own first service `X-API-Key`. So cost events persist as long as
  `ECP_JWT_SECRET` is set and at least one Zeroth service key
  (`ZEROTH_SERVICE_API_KEYS_JSON`) is configured. With no Zeroth service key the
  self-calls carry only the Bearer; in the gated in-process topology that yields
  `401` → fail-open (events drop), so configure a service key for in-process use.
  (`ZEROTH_REGULUS__API_KEY` remains unused — the bundled flow does not need it.)
- **Reaching `/regulus` externally** requires Zeroth's `X-API-Key` (no bypass);
  econ's open token issuer is therefore *not* internet-exposed by enabling the
  mount.
- **Settings isolation.** Zeroth uses the `ZEROTH_` prefix; the bundled backend
  uses `ECP_`. They do not collide.
- **Migrations.** SQLite needs none (schema is created at startup). The Alembic
  chain under `src/zeroth/econ_plane/_migrations` is for offline Postgres ops only.

## Related references

- [Economics concept page](../../concepts/econ.md)
- [Configuration Reference](../../reference/configuration.md)
- Provenance: `src/zeroth/core/econ/instrumentation/PROVENANCE.md`,
  `src/zeroth/econ_plane/PROVENANCE.md`

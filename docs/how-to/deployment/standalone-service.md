# Standalone service

Standalone service mode runs `zeroth-core` as a production single-node deploy
fronted by a reverse proxy. Use it when you want real Postgres, TLS, and a
process manager (systemd, supervisord, or a pod) — but without the overhead
of Docker Compose or Kubernetes.

## Use case

- Production single-node deploy on a VM or bare-metal host
- Fronted by nginx or Caddy for TLS termination
- Managed by systemd (or another process supervisor)
- Postgres for durable state, optional Redis for arq dispatch

## Prerequisites

- Python 3.12+
- Postgres 14+ with a database and role provisioned
- (Optional) Redis for `ZEROTH_DISPATCH__ARQ_ENABLED=true`
- A reverse proxy (nginx, Caddy, or Traefik) terminating TLS
- A secret-management story for `ZEROTH_*` env vars

## Install

```bash
python3.12 -m venv /opt/zeroth/venv
/opt/zeroth/venv/bin/pip install "zeroth-core[memory-pg,dispatch]"
```

## Configure

Create `/etc/zeroth/zeroth.env` with at least the following (excerpt — see the
[full Configuration Reference](../../reference/configuration.md)):

```bash
ZEROTH_DATABASE__BACKEND=postgres
ZEROTH_DATABASE__POSTGRES_DSN=postgresql://zeroth:secret@db:5432/zeroth
ZEROTH_DATABASE__ENCRYPTION_KEY=<base64-32-bytes>
ZEROTH_AUTH__API_KEYS_JSON={"ops":"<opaque-token>"}
ZEROTH_DISPATCH__ARQ_ENABLED=true
ZEROTH_REDIS__HOST=127.0.0.1
ZEROTH_REDIS__PORT=6379
OPENAI_API_KEY=sk-...
```

Run Alembic before the first start:

```bash
/opt/zeroth/venv/bin/alembic upgrade head
```

## Run

The `zeroth-core serve` console script wraps uvicorn with TLS + migration
logic from `zeroth.core.service.entrypoint`. For systemd control, call
uvicorn directly against the `app_factory`:

```bash
/opt/zeroth/venv/bin/uvicorn zeroth.core.service.entrypoint:app_factory \
  --factory \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 4 \
  --proxy-headers
```

The `--factory` flag is required — the entrypoint exposes `app_factory`
rather than a module-level `app`, so the settings + database bootstrap runs
once per worker.

### systemd unit

```ini
[Unit]
Description=Zeroth Core API
After=network.target postgresql.service

[Service]
Type=simple
User=zeroth
EnvironmentFile=/etc/zeroth/zeroth.env
ExecStart=/opt/zeroth/venv/bin/uvicorn zeroth.core.service.entrypoint:app_factory \
  --factory --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### nginx front

```nginx
server {
  listen 443 ssl http2;
  server_name api.example.com;

  ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

## Verify

```bash
curl -I https://api.example.com/healthz
systemctl status zeroth
```

See the [HTTP API Reference](../../reference/http-api.md) for the full
surface you can now exercise through the proxy.

## Common gotchas

- **Missing `--factory`:** without it, uvicorn imports `app_factory` as a
  module attribute and the service never bootstraps.
- **Migrations not run:** the `zeroth-core serve` console script runs
  `alembic upgrade head` automatically; raw `uvicorn` does not. Run it once
  before first start, and after every upgrade.
- **Encryption key:** `ZEROTH_DATABASE__ENCRYPTION_KEY` must be stable across
  restarts — losing it makes previously stored secrets unrecoverable.
- **Worker count:** start with `--workers $(nproc)` and tune based on
  observed latency; agent runs are async-heavy so CPU is rarely the bottle.

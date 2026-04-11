# With the Regulus companion service

Regulus is the economics companion service that enforces cost budgets and
tracks spend across graph runs. `zeroth-core` integrates with it through the
`econ-instrumentation-sdk` (pinned to `>=0.1.1`). Enabling Regulus turns on
cost checks at agent nodes and halts runs that would exceed their declared
budget.

## Use case

- Enforcing per-run, per-tenant, or per-graph cost caps
- Tracking spend for billing or chargeback
- Fail-closed guarantees on cost-checked nodes
- Centralizing economics across multiple `zeroth-core` deployments

## Prerequisites

- A running Regulus service reachable over HTTP
- An API key issued by Regulus
- `zeroth-core >= 0.1.1` (the SDK dependency is already transitive)

## Install

No extra install is required — `econ-instrumentation-sdk` ships as part of
the `zeroth-core` dependency set.

```bash
pip install zeroth-core
```

## Configure

Set the following env vars (see
[Configuration Reference](../../reference/configuration.md) for the full
`regulus` section):

```bash
ZEROTH_REGULUS__ENABLED=true
ZEROTH_REGULUS__BASE_URL=http://regulus:8080/v1
ZEROTH_REGULUS__API_KEY=<regulus-token>
```

When `ZEROTH_REGULUS__ENABLED=true`, agent nodes are wrapped in a cost check
before execution. The orchestrator halts the run if the budget is exceeded,
and the halt is recorded in the audit trail.

## Docker Compose excerpt

Add a `regulus` service alongside `zeroth` in your compose file. The
bundled `docker-compose.yml` already wires this for you — here is the
salient excerpt:

```yaml
services:
  zeroth:
    environment:
      ZEROTH_REGULUS__ENABLED: "true"
      ZEROTH_REGULUS__BASE_URL: "http://regulus:8080/v1"
    depends_on:
      - regulus

  regulus:
    image: regulus-backend:latest
    environment:
      REGULUS_PORT: "8080"
    networks:
      - zeroth-net
```

For the full working file, see
[Docker Compose deployment](docker-compose.md).

## Standalone deployment

If you run `zeroth-core` as a [standalone service](standalone-service.md),
add the same three env vars to `/etc/zeroth/zeroth.env`:

```bash
ZEROTH_REGULUS__ENABLED=true
ZEROTH_REGULUS__BASE_URL=https://regulus.internal.example.com/v1
ZEROTH_REGULUS__API_KEY=<regulus-token>
```

## Verify

1. Start a graph run with a cost-capped contract (see the
   [budget cap cookbook recipe](../cookbook/budget-cap.md)).
2. Watch the orchestrator halt the run when the budget is exceeded.
3. Inspect the audit trail — the `econ.halt` event carries the Regulus
   decision metadata.

```bash
curl -f http://localhost:8000/healthz
curl -s http://regulus:8080/healthz
```

## Common gotchas

- **Fail-closed on unreachable Regulus:** if `ZEROTH_REGULUS__ENABLED=true`
  and Regulus is unreachable, cost-checked nodes fail closed. Disable the
  integration or fix connectivity before running production traffic.
- **Version pinning:** confirm `econ-instrumentation-sdk>=0.1.1` is resolved
  in your lockfile. This is the pin set in Phase 28 when the SDK moved from
  a local path dep to PyPI.
- **Clock skew:** Regulus uses signed budget windows. Keep NTP running on
  both hosts or you will see spurious "budget expired" halts.
- **API key leakage:** `ZEROTH_REGULUS__API_KEY` is a secret. Put it in your
  secret store, not in the compose file.

## Related references

- [Economics concept page](../../concepts/econ.md)
- [Python API Reference — econ](../../reference/python-api/econ.md)
- [Configuration Reference](../../reference/configuration.md)

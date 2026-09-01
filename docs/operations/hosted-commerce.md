# Operate hosted identity and commerce

Zeroth Cloud uses WorkOS AuthKit for users, organizations, roles, and sealed
browser sessions. Paddle is the merchant of record and the only authority that
changes a paid subscription. These adapters are optional and are not installed
with the SDK or default open-source runtime.

## Install and configure

Install the hosted adapter dependencies with `uv sync --extra cloud`. Set these
environment variables in the hosted API process:

```text
ECP_WORKOS_AUTHKIT_ENABLED=true
ECP_WORKOS_CLIENT_ID=...
ECP_WORKOS_API_KEY=...
ECP_WORKOS_REDIRECT_URI=https://api.example.com/v1/cloud/auth/callback
ECP_WORKOS_COOKIE_PASSWORD=<at least 32 random characters>
ECP_CLOUD_BROWSER_ORIGIN=https://api.example.com

ECP_PADDLE_BILLING_ENABLED=true
ECP_PADDLE_API_KEY=...
ECP_PADDLE_WEBHOOK_SECRET=...
ECP_PADDLE_SANDBOX=true
ECP_PADDLE_SOLO_PRICE_ID=pri_...

ECP_CLOUD_ENTITLEMENTS_ENABLED=true
ECP_CLOUD_SCHEDULER_ENABLED=true
# ECP_CLOUD_SCHEDULER_INTERVAL_SECONDS=60
```

The hosted image runs the narrowed economic plane directly at the public root;
it does not start the full Zeroth runtime or bundle the open-source console.
The plane validates these settings before accepting traffic. Once WorkOS,
Paddle, or cloud entitlements are enabled, any invalid hosted setting or schema
initialization failure aborts startup. The default open-source runtime and UI
remain separate and unchanged.

Production must also replace `ECP_JWT_SECRET`, use HTTPS, run the economic
migration chain with `zeroth-core migrate-econ`, and set
`ECP_PADDLE_SANDBOX=false` only after the production catalog and webhook
destination exist. The hosted SKU does not create the broader runtime's service
tables or `alembic_version`; its schema authority is `alembic_version_econ`.
The single API replica runs the database-claimed decision scheduler in-process.
If that task exits, `/health/ready` reports a degraded body so Railway replaces
the replica. A future multi-replica deployment may run the same loop safely
because schedule claims are conditional database updates, but the launch shape
deliberately remains one replica.

## Runtime flow

1. `GET /` presents the single Solo offer and links to AuthKit.
2. `GET /v1/cloud/auth/login` starts AuthKit authorization-code login with PKCE.
3. `GET /v1/cloud/auth/callback` verifies state, creates a WorkOS organization
   for a first-time solo account when necessary, and binds its immutable ID to
   one local tenant. Browser navigation receives a one-time activation page;
   API callers retain the JSON response.
4. The activation transaction creates one 14-day trial and one Analyst
   quickstart API key. The plaintext key is shown once and never stored.
5. The account page posts to `/account/checkout`, or an Admin API caller uses
   `POST /v1/cloud/billing/checkout` with
   `{"plan":"solo"}`. The server chooses the Paddle price and trusted tenant
   metadata. Team is deliberately not a purchasable self-serve plan yet.
6. Paddle sends subscription events to
   `POST /v1/cloud/billing/paddle/webhook`. Only a raw-body signature-verified
   event updates the local entitlement projection.
7. `/account` shows plan status, aggregate meter usage, key fingerprints, and
   replacement/revocation controls. It hands paid users to Paddle's short-lived
   customer portal; it never displays a stored key or payment data.

## Security and failure invariants

- WorkOS organization ID owns tenant scope; email and request fields do not.
- Unknown WorkOS role slugs are rejected. Configure `admin`, `analyst`,
  `approver`, and `viewer` in WorkOS when those roles are used.
- Checkout redirects never activate access. Delayed or failed webhooks leave
  the prior local subscription state in place and must be retried by Paddle.
- Paddle event IDs are replay-safe. A changed replay is treated as a conflict;
  older and equal-time events cannot roll entitlement state backward.
- A webhook with an unknown price, missing tenant metadata, invalid period, or
  invalid signature is rejected without changing subscription state.
- Rollback of revision `20260901_17` removes only external identity bindings;
  it deliberately preserves subscription and billing evidence.
- A Paddle subscription in `trialing` state always receives Trial quotas even
  though its catalog plan is Solo. Solo quotas begin only after Paddle reports
  `active`; checkout cannot prematurely expand free usage.

## Approved launch offer

Solo is the only purchasable plan at launch: **$39/month after a 14-day
trial**. Its enforceable billing-period limits are 100,000 ingested events, 31
decision scans, three hosted backtests, 300 provider-call credits across those
backtests, and five daily schedules. A backtest reserves its count and provider
calls atomically; if either allowance is exhausted, neither meter advances.
Unused call credits are returned after execution.

The trial permits one hosted backtest and 100 provider-call credits. Team and
Scale remain internal entitlement shapes for compatibility and future
expansion, but checkout rejects them. Do not advertise or sell Team until
member, governance, and collaboration limits are enforced rather than merely
described.

The Paddle Solo price must itself be a monthly USD 39 recurring price with a
free 14-day trial that requires a payment method. The local trial makes first
value available before checkout; the Paddle trial is what guarantees that a
developer who continues through checkout is not charged before day 14. Reject
the launch if the configured price has different renewal or trial terms.

## Railway deployment

The repository does not publish Railway project state or infrastructure as
code. Create one managed Postgres service and one headless economic-plane API
built from `Dockerfile.cloud` in the Railway project itself. Configure
`zeroth-core migrate-econ` as the pre-deploy command and use `/health/ready` as
the healthcheck. Inspect its JSON as well as its HTTP status because readiness
intentionally reports dependency degradation in the body.

Set the WorkOS and Paddle values listed above, plus `ECP_JWT_SECRET`, on the API
service. Wire the managed Postgres URL to `ECP_DATABASE_URL`, set
`ECP_CLOUD_SCHEDULER_ENABLED=true`, and retain the single-replica launch shape.
Railway should start the image using the `CMD` already declared by
`Dockerfile.cloud`:

```bash
docker build -f Dockerfile.cloud -t zeroth-cloud:candidate .
docker inspect zeroth-cloud:candidate --format '{{json .Config.Cmd}}'
```

Review the Railway project configuration before deploying. No repository check
proves that the managed database, domain, region, backups, variables, or remote
deployment exist.

## Read-only vendor configuration audit

After creating the WorkOS application and Paddle catalog, verify their actual
vendor-side configuration before deploying or opening checkout. The audit uses
only `GET` requests and writes a secret-free mode-0600 report:

```bash
export ECP_WORKOS_API_KEY='...'
export ECP_WORKOS_REDIRECT_URI='https://api.example.com/v1/cloud/auth/callback'
export ECP_PADDLE_API_KEY='...'
export ECP_PADDLE_SOLO_PRICE_ID='pri_...'
export ZEROTH_LAUNCH_PADDLE_NOTIFICATION_SETTING_ID='ntfset_...'

uv run python release/cloud_vendor_readiness.py \
  --public-origin https://api.example.com \
  --sandbox \
  --output .evidence/cloud-vendor-sandbox.json

unset ECP_WORKOS_API_KEY ECP_PADDLE_API_KEY
```

Use `--production` with production-scoped credentials for the production
record. The report fails unless the exact AuthKit callback is registered, the
WorkOS `admin` role exists, the configured Paddle price is an active standard
USD 39 monthly price with a free 14-day payment-method trial, and the Paddle
notification destination is active at Zeroth's exact webhook URL, excludes
sensitive fields, and subscribes to every subscription lifecycle event used by
the entitlement projection. Analyst, Approver, and Viewer are not launch
requirements for Solo; configure them only when those WorkOS browser roles are
actually offered.

This audit cannot prove that the runtime WorkOS client ID and API key belong to
the same application or that the stored Paddle webhook secret matches the
destination's write-only secret. The end-to-end sandbox signup and simulated
signed webhook remain mandatory for those boundaries.

## Production acceptance

The repository-level commercial-flow acceptance test composes the real Zeroth
routes, persistence, session authentication, project-key authentication,
entitlement meter, and billing projection in one process:

```bash
uv run pytest -q tests/acceptance/test_cloud_commercial_flow.py
```

It proves that signup creates a trial and one-time key, the actual lean
`zeroth-sdk` client can use that key for the first-value backtest and retained
history, only a signed Paddle event upgrades the subscription, the WorkOS Admin
can open the portal, and cancellation makes a fresh SDK backtest return HTTP
402. WorkOS code exchange and Paddle network/signature verification are
deterministic fakes in this test; it is not evidence that either production
project or public callback is configured.

After deploying a candidate and creating a one-time project key, run the
read-only black-box preflight:

```bash
export ZEROTH_CLOUD_API_KEY='<one-time project key>'
uv run python release/cloud_launch_preflight.py \
  --base-url https://api.example.com \
  --output .evidence/cloud-preflight.json
unset ZEROTH_CLOUD_API_KEY
```

It verifies the public Solo page, strict readiness body and schema revision, the
AuthKit redirect and secure flow-cookie attributes, and authenticated
backtest-history access. It sends only `GET` requests, never follows the AuthKit
redirect, never creates a backtest or Paddle transaction, and excludes the
project key, cookie, and authorization state from the JSON report. A passing
report is necessary but does not replace a real purchase journey.

Before selling access, complete one sandbox and one production run covering:

```text
AuthKit signup → organization binding → one-time key → bounded backtest
→ Paddle checkout → signed active webhook → paid entitlement
→ customer portal → cancel/pause webhook → entitlement revoked
```

Record Paddle and WorkOS event identifiers, local tenant ID, subscription
projection, and the backtest decision. Never record API keys, session cookies,
vendor secrets, prompts, or customer payloads.

Use the [cloud launch evidence record](cloud-launch-evidence.md) for both runs.
Before the production run, resolve and publish every item in the
[launch policy inputs](cloud-launch-policy-inputs.md); the checklist is not a
substitute for owner or counsel approval.

After opening checkout, use the [Solo SaaS launch runbook](solo-saas-launch.md)
and write a rolling aggregate funnel report without identity or payload fields:

```bash
uv run python release/cloud_funnel_report.py \
  --window-days 30 \
  --output .evidence/cloud-funnel.json
```

This report measures signups, first and repeat backtests, checkout-completed
subscriptions, paid activation, cancellation, past-due state, verdict mix, and
time to first value from existing server-owned records. It does not install a
tracking pixel, count page views, or export tenant identifiers.

## Launch gates still requiring an owner or external system

- Create the monthly $39 Solo product and price in Paddle with a free 14-day
  payment-method trial; configure the default payment link and `/account` return
  path, and leave Team disabled.
- Link the Railway project, select its region/domain, review its service
  configuration, and provision paid compute plus Postgres backups and recovery.
- Configure WorkOS production redirect/origin values and the supported role
  slugs. A WorkOS custom auth domain is optional, not a launch dependency.
- Configure Paddle sandbox products, prices, webhook destination, customer
  portal, refund policy, and then the corresponding production catalog.
- Publish terms, privacy/data-deletion policy, refund policy, and one support
  address before accepting a production payment.
- Complete the sandbox and production journeys above and retain only their
  non-secret identifiers as launch evidence.

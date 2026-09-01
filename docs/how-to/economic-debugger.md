# Run the economic debugger API

The current product slice is a headless, self-hostable API. It does not require
the console package and does not depend on the release-blocked `zeroth-sdk`
prototype.

## Install and migrate

Install the backend extra from a source checkout without the UI:

```bash
pip install -e ".[regulus]"
# or, with the repository's locked environment:
uv sync --extra regulus
```

From a source checkout, configure a durable database and signing secret, then
apply the economic-plane migration chain:

```bash
export ECP_DATABASE_URL=sqlite+pysqlite:////var/lib/zeroth/econ_plane.db
export ECP_JWT_SECRET=<a-persistent-random-secret>
export ECP_SERVICE_PRINCIPAL_TENANT_ID=acme
uv run alembic -c alembic-econ.ini upgrade head
uv run uvicorn zeroth.econ.plane.main:app --host 127.0.0.1 --port 8001
```

Use PostgreSQL for a managed or multi-process deployment. SQLite startup can
converge supported historical schemas, but operators should still apply
migrations explicitly. `/health` reports `schema_revision.state=current` when
the database is ready.

## Authenticate

All ingestion and debugger routes require an econ-plane JWT. Zeroth's bundled
runtime mints short-lived service tokens automatically. For a local API probe,
mint one from the configured service identity:

```bash
export TOKEN="$(uv run python -c 'from zeroth.econ.analytics.service_auth import mint_econ_service_token; print(mint_econ_service_token() or "")')"
```

The JWT tenant claim—not a request field—selects the data boundary. `Admin` and
`Analyst` may ingest; `Viewer`, `Approver`, `Analyst`, and `Admin` may query.

## Define a successful outcome

Before the debugger can calculate cost per successful outcome, an Admin must
bind each workflow version to one terminal outcome type and predicate:

```bash
curl -sS http://127.0.0.1:8001/v1/debugger/outcome-definitions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow_id": "invoice-processing",
    "workflow_version": "v3",
    "outcome_type": "approval",
    "operator": "equals",
    "target": true
  }'
```

Definitions are tenant-scoped and immutable for a workflow version. Replaying
the exact definition is idempotent; changing the outcome type, operator, or
target returns `409`. Publish a new workflow version to change success
semantics. Supported operators are `equals`, `not_equals`,
`greater_than_or_equal`, and `less_than_or_equal`; ordered comparisons require
a numeric target. This covers definitions such as `fraud_flag == false` and
`reopen_rate <= 0.05` without guessing what a raw value means.

List the definitions visible to the current tenant:

```bash
curl -sS -G http://127.0.0.1:8001/v1/debugger/outcome-definitions \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode workflow_id=invoice-processing
```

## Ingest economic evidence

An execution identifies the workflow, immutable version, run, step, attempt,
analyzed subject, and bounded typed dimensions. Keep measured and estimated
costs explicit. The shipped client uses the same contract. It can require both
writes to be accepted before returning:

```python
import os
from datetime import UTC, datetime
from decimal import Decimal

from zeroth.econ.instrumentation import (
    ExecutionEvent,
    InstrumentationClient,
    OutcomeEvent,
)

with InstrumentationClient.authenticated(
    base_url="http://127.0.0.1:8001/v1",
    bearer_token=os.environ["ZEROTH_ECON_TOKEN"],
) as econ:
    econ.track_execution_confirmed(
        ExecutionEvent(
            execution_id="evt-001",
            join_key="run-001",
            timestamp=datetime.now(UTC),
            capability_id="invoice-processing",
            implementation_id="invoice-processing:v3",
            model_version="gpt-5-mini",
            workflow_id="invoice-processing",
            workflow_version="v3",
            run_id="run-001",
            step_id="extract",
            attempt=1,
            subject_id="account-42",
            dimensions={"plan": "enterprise", "region": "us-east"},
            token_cost_usd=Decimal("0.0125"),
            cost_measurement="measured",
            usage_measurement="measured",
            metadata={
                "provider": "openai",
                "model": "gpt-5-mini",
                "project_id": "proj_a",
            },
        )
    )
    econ.track_outcome_confirmed(
        OutcomeEvent(
            execution_id="evt-001",
            join_key="run-001",
            capability_id="invoice-processing",
            outcome_type="approval",
            outcome_value=True,
        )
    )
```

Confirmed delivery raises when the plane rejects a write, so a setup check
cannot silently report success while evidence remains only in memory. The
ordinary `track_execution` and `track_outcome` methods remain buffered for
long-running applications.

Do not put the token in source code. For global instrumentation helpers, set
`ECP_BASE_URL` and `ECP_BEARER_TOKEN`, then call
`configure(InstrumentationConfig.from_env())`; the runtime reads the token from
the environment. Choose one authentication path: use
`InstrumentationClient.authenticated` for a short-lived static token, or the
ordinary constructor's `headers_provider` in a long-running service when
credentials must rotate.

### Equivalent HTTP contract

The execution request emitted by the client is equivalent to:

```bash
curl -sS http://127.0.0.1:8001/v1/instrumentation/executions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "execution_id": "evt-001",
    "join_key": "run-001",
    "timestamp": "2026-08-30T12:00:00Z",
    "capability_id": "invoice-processing",
    "implementation_id": "invoice-processing:v3",
    "model_version": "gpt-5-mini",
    "workflow_id": "invoice-processing",
    "workflow_version": "v3",
    "run_id": "run-001",
    "step_id": "extract",
    "attempt": 1,
    "subject_id": "account-42",
    "dimensions": {"plan": "enterprise", "region": "us-east"},
    "token_cost_usd": "0.0125",
    "cost_measurement": "measured",
    "usage_measurement": "measured"
  }'
```

The terminal outcome request uses the same run identity:

```bash
curl -sS http://127.0.0.1:8001/v1/instrumentation/outcomes \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "execution_id": "evt-001",
    "join_key": "run-001",
    "capability_id": "invoice-processing",
    "implementation_id": "invoice-processing:v3",
    "outcome_type": "approval",
    "outcome_value": true,
    "occurred_at": "2026-08-30T12:00:01Z"
  }'
```

Replaying the same execution identity and immutable payload reports
`duplicate`; changing its identity-bearing fields reports a validation error
instead of double-counting spend.

## Query the debugger

```bash
curl -sS -G http://127.0.0.1:8001/v1/debugger/timeline \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode workflow_id=invoice-processing

curl -sS -G http://127.0.0.1:8001/v1/debugger/cohorts \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode workflow_id=invoice-processing \
  --data-urlencode group_by=dimension \
  --data-urlencode dimension=plan

curl -sS -G http://127.0.0.1:8001/v1/debugger/breakage \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode workflow_id=invoice-processing

curl -sS -G http://127.0.0.1:8001/v1/debugger/report \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode workflow_id=invoice-processing \
  --data-urlencode cohort_dimension=plan
```

`start` is inclusive and `end` is exclusive. Timeline and cohort results keep
measured and estimated dollars separate and report incomplete evidence.
Breakage reports money present in failed runs at each observed step; its
`attribution` value is `failed_run_exposure_not_step_causality`. Do not describe
that number as proof that the step caused the failure.

Each request scans at most 50,000 recent execution events. That bound is for
single-team debugging. Organization history, scheduled reports, chargeback,
and provider-bill reconciliation belong in pre-aggregated managed storage.

## Generate a shareable local diagnostic

Keep the JWT in an environment variable and render the API response to
Markdown without installing or launching the UI:

```bash
export ZEROTH_ECON_TOKEN="$TOKEN"
uv run zeroth-econ diagnose \
  --workflow-id invoice-processing \
  --cohort-dimension plan \
  --output economic-diagnostic.md
```

Use `--format json` for automation. The report returns `404` rather than a
zero-value story when the selected workflow and window have no evidence. It
chooses one next action, keeps measured and estimated cost separate, and embeds
the same claim limits as the API. It never labels failed-run exposure as causal
waste or historical evidence as proven savings. If a version has no outcome
definition, the report names it under `undefined_outcome_versions`, marks its
runs unresolved, and recommends defining success before changing the workflow.

## Debug and roll back

- A `401` means the JWT is missing, invalid, or expired.
- A `403` means the authenticated role is insufficient or the payload claims a
  different tenant.
- A `422` on ingestion usually means inconsistent identity, an invalid
  dimension, or a missing capability relationship.
- A result with `incomplete_events > 0` means cost or identity evidence is
  absent; it is not zero spend.
- Check `/health`, application logs, and the `alembic_version` row before
  investigating query totals.

Revisions `20260830_11`, `20260830_12`, and `20260830_13` are additive. Before
rollback, stop serving the affected routes and back up the database. Downgrade
to `20260830_12` to remove only provider-bill reconciliation, as described in
the [reconciliation guide](provider-bill-reconciliation.md). To remove outcome
definitions as well, run:

```bash
uv run alembic -c alembic-econ.ini downgrade 20260830_11
```

This drops the provider-bill tables and `outcome_definitions`; historical
execution and outcome rows remain, but all workflow versions become unresolved
until definitions are restored.
To remove the complete debugger evidence spine as well, then run:

```bash
uv run alembic -c alembic-econ.ini downgrade 20260824_10
```

That second downgrade removes the debugger indexes and evidence-spine columns;
historical rows remain, but their new identity fields are lost. Roll forward
with `upgrade head` before re-enabling the routes.

## Commercial activation trigger

Do not meter these debugger queries. Offer the free layer to production teams
and record requests for cross-team rollups, provider-invoice reconciliation,
chargeback, retention, SSO/SCIM, or signed change evidence. The first normalized
provider-bill API now exists; use the
[reconciliation guide](provider-bill-reconciliation.md) to test it with a real
export. Do not expand into credentialed connectors, billing, or the organization
shell until a qualified buyer validates this closure report. The safer fallback
is managed hosting plus SSO/RBAC/retention if reconciliation demand does not
appear.
Use the [commercial pilot runbook](../operations/economic-debugger-commercial-pilot.md)
for the privacy-safe, asynchronous qualification funnel and stop criteria.

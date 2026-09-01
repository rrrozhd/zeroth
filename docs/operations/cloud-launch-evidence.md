# Cloud launch evidence record

Use one copy of this record for the Paddle sandbox journey and another for the
production journey. Store only operational identifiers needed to reconstruct
the result. Never store API keys, session cookies, vendor secrets, AuthKit state,
prompts, traces, model inputs or outputs, or customer payloads in this record.

## Candidate

```text
Environment: [sandbox|production]
Candidate commit: [GIT_SHA]
Container image digest: [IMAGE_DIGEST]
Railway project/service/deployment IDs: [NON_SECRET_IDS]
Public API origin: [HTTPS_ORIGIN]
Region: [REGION]
Started at (UTC): [TIMESTAMP]
Operator: [OWNER]
```

## Read-only preflight

```text
Vendor readiness report path: [PATH]
Vendor readiness SHA-256: [DIGEST]
Vendor readiness result: [passed|failed]
Report path: [PATH]
SHA-256: [DIGEST]
Result: [passed|failed]
Readiness schema revision: [REVISION]
```

The vendor report and deployment preflight must each show all checks as passed.
Record both digests after the commands finish; do not edit a report in place.

## Commercial journey

```text
WorkOS user ID: [ID]
WorkOS organization ID: [ID]
Local tenant ID: [ID]
Project-key ID or fingerprint (never the key): [ID]
Trial activation result: [RESULT]
Bounded first-value backtest ID: [ID]
Backtest decision/result: [RESULT]
Paddle product ID: [ID]
Paddle price ID: [ID]
Paddle transaction ID: [ID]
Paddle subscription ID: [ID]
Paddle activation event ID: [ID]
Paid entitlement observed at (UTC): [TIMESTAMP]
Customer portal opened: [yes|no]
Cancellation or pause event ID: [ID]
Revoked entitlement observed at (UTC): [TIMESTAMP]
Fresh backtest after revocation returned HTTP 402: [yes|no]
Finished at (UTC): [TIMESTAMP]
```

## Failure and recovery evidence

```text
Failed step (or none): [STEP]
Externally visible error: [REDACTED_ERROR_CLASS]
Retry/replay ID: [ID]
Entitlement before retry: [STATE]
Entitlement after retry: [STATE]
Incident or follow-up issue: [LINK_OR_ID]
```

Do not copy raw webhook bodies or headers. A replay test should use the vendor
event ID and the resulting projection state, not retained payment payloads.

## Sign-off

```text
Engineering owner: [NAME / DATE / RESULT]
Commercial owner: [NAME / DATE / RESULT]
Policy pages reviewed and public: [URLS]
Support address tested: [ADDRESS / DATE]
Launch decision: [go|no-go]
Residual risks accepted: [RISKS]
```

A production `go` requires the sandbox and production journeys to pass. A
passing repository acceptance test or read-only preflight is not enough.

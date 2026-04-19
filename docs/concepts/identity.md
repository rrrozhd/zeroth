# Identity

## What it is

**Identity** is the tiny shared package that defines *who* is acting on the Zeroth service: the `ActorIdentity` recorded on runs, approvals, and audits, and the `AuthenticatedPrincipal` returned by the service auth layer.

It is the single source of truth for subject, auth method, role set, and tenant/workspace scope across the rest of the runtime. There is deliberately no policy logic here — just the data shapes that other subsystems depend on.

## Why it exists

Every governance claim Zeroth makes ("this run was approved by operator X", "these audit records belong to tenant Y") needs a stable identity shape. If each subsystem rolled its own user model, you'd end up with mismatched fields and leaks at the seams.

Instead, `zeroth.core.identity` defines one principal model that `service.auth` returns, `runs` records on each run, `approvals` stamps into every `ApprovalResolution`, and `audit` embeds in every `NodeAuditRecord.actor`. Changing roles once changes them everywhere — and the same model is used whether a caller authenticated via a static API key or a JWT bearer token.

Keeping the model tiny also keeps the identity package free of framework dependencies: anything that needs to *describe* an actor can depend on `zeroth.core.identity` without pulling in FastAPI or the auth stack.

## Where it fits

Identity is consumed on both sides of the request boundary.

On the inbound side, `zeroth.core.service.auth` reads `ServiceAuthConfig` (API keys or JWT bearer settings), verifies the credential, and returns an `AuthenticatedPrincipal`. That principal is scoped to a tenant and workspace via `PrincipalScope`.

On the outbound side, the principal is downgraded to `ActorIdentity` (request-only claims stripped) and handed to [runs](runs.md), [approvals](approvals.md), and [audit](audit.md) so each record can faithfully answer "who did this?". The [approvals](approvals.md) API uses the role set to authorize `resolve`; the [service](service.md) API enforces the same roles on every route.

## Key types

- **`ActorIdentity`** — the stable, persistable identity: `subject`, `auth_method`, `roles`, `tenant_id`, `workspace_id`.
- **`AuthenticatedPrincipal`** — `ActorIdentity` plus request-only `credential_id` and raw `claims`; exposes `scope()` and `to_actor()`.
- **`AuthMethod`** — `API_KEY` or `BEARER`.
- **`ServiceRole`** — `OPERATOR`, `REVIEWER`, `ADMIN` (used by route authorization).
- **`PrincipalScope`** — the tenant / workspace scope attached to a principal.

## See also

- [Usage Guide: identity](../how-to/identity.md) — configure API-key and bearer credentials for the service.
- [Concept: approvals](approvals.md) — approval decisions carry the resolver's `ActorIdentity`.
- [Concept: service](service.md) — `service.auth` is where principals are minted; routes enforce `ServiceRole`.
- [Concept: audit](audit.md) — every `NodeAuditRecord.actor` is an `ActorIdentity`.
- [Concept: runs](runs.md) — runs carry the submitting principal's `ActorIdentity` for the lifetime of execution.
- [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md) — end-to-end policy + approval + audit story.

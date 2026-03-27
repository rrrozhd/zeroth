# Phase 6 Identity And Access Model

## Authentication

Every external service request is authenticated before route logic runs.

- `X-API-Key` resolves a static principal configured in service auth settings.
- `Authorization: Bearer <token>` resolves a JWT/OIDC-backed principal verified against configured issuer, audience, and JWKS metadata.
- Unauthenticated requests are rejected with `401` and recorded as `service.auth` audit events.

## Principal Model

The authenticated principal shape is shared across the service, approvals, runs, and audits.

- `subject`: stable caller identity
- `auth_method`: `api_key` or `bearer`
- `roles`: service roles used for route authorization
- `tenant_id`: required tenant scope
- `workspace_id`: optional workspace scope

Approval and audit surfaces persist actor lineage as structured identity records rather than free-form strings.

## Authorization

Phase 6 enforces the following permission vocabulary:

- `deployment:read`
- `run:create`
- `run:read`
- `approval:read`
- `approval:resolve`
- `audit:read`

Role mapping:

- `operator`: deployment read, run create/read, approval read
- `reviewer`: deployment read, run read, approval read/resolve
- `admin`: all permissions

Permission denials are rejected with `403` and recorded as `service.authorization` audit events.

## Tenant And Workspace Isolation

Deployments, runs, threads, approvals, and audits now carry tenant/workspace scope.

- `tenant_id` is persisted on every governed object.
- `workspace_id` is persisted when present.
- Deployment scope is stamped from deployment settings and inherited by runs, threads, approvals, and audit records.
- Cross-tenant or cross-workspace access attempts are hidden or rejected at the service edge and audited.

Legacy rows are backfilled to `tenant_id="default"` and `workspace_id=NULL` so pre-Phase-6 data continues to load.

## Public Lineage Surfaces

Run responses now expose:

- `submitted_by`
- `tenant_id`
- `workspace_id`

Approval responses now expose:

- `requested_by`
- `resolution.actor`
- `tenant_id`
- `workspace_id`

Audit records and approval actions retain the same actor/scope lineage for downstream governance phases.

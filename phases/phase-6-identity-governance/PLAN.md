# Phase 6 — Identity & Tenant Governance Plan

## Goal

Close the platform trust-boundary gaps by authenticating every caller, attributing every sensitive action to a real principal, and isolating tenants end to end across the service, runtime, and persistence layers.

## Why This Phase Exists

The MVP service wrapper is deployment-scoped and operational, but it still assumes a trusted caller. Public routes do not yet establish an authenticated principal, approval resolution accepts a caller-supplied approver string, and the persistence model does not enforce tenant or workspace boundaries. That is acceptable for an MVP but not for the governed production target Zeroth is intended to become.

## Scope

- Service-layer authentication for all public routes
- Principal and actor model shared by runs, approvals, and audits
- Authorization and least-privilege route enforcement
- Tenant and workspace scoping on persisted platform objects
- Cross-tenant rejection and attribution
- Public API contract changes needed to expose identity lineage safely

## Out Of Scope

- End-user UI or admin console work
- Full enterprise SSO productization beyond the auth abstraction and one or two concrete adapters
- Billing and commercial account management

## Relevant Code Areas

- `src/zeroth/service/app.py`
- `src/zeroth/service/run_api.py`
- `src/zeroth/service/approval_api.py`
- `src/zeroth/service/contracts_api.py`
- `src/zeroth/service/bootstrap.py`
- `src/zeroth/deployments/models.py`
- `src/zeroth/runs/models.py`
- `src/zeroth/runs/repository.py`
- `src/zeroth/approvals/models.py`
- `src/zeroth/approvals/service.py`
- `src/zeroth/audit/models.py`
- `src/zeroth/audit/repository.py`
- `tests/service/`
- `tests/approvals/`
- `tests/audit/`

## Workstreams

### 6A. Service Authentication Boundary

Implement a service authentication layer that resolves an authenticated principal before route handlers run.

Requirements:

- Define a principal model with stable subject, auth method, and optional tenant/workspace claims
- Add a service auth interface that can support API keys now and bearer-token or OIDC-backed adapters later
- Reject unauthenticated requests at the edge instead of letting handlers infer trust from request payloads
- Attach the resolved principal to request context so routes, approvals, and audit writers can reuse it
- Replace caller-supplied approval attribution with principal-derived attribution

### 6B. Authorization And Role Model

Introduce explicit authorization rules for platform resources instead of relying on deployment scoping alone.

Requirements:

- Define roles and permissions for deployment metadata, run invocation, run inspection, approval review, approval resolution, and audit access
- Enforce authorization in the service layer for every route, including read-only metadata routes
- Enforce least privilege so reviewers cannot perform operator actions and operators cannot read data outside their scope
- Record authorization failures in audit-friendly form

### 6C. Tenant And Workspace Isolation

Extend persistence and query flows so data is isolated by tenant or workspace instead of only by deployment ref.

Requirements:

- Add tenant and workspace identifiers to deployments, runs, threads, approvals, and audits
- Propagate tenant scope through bootstrap, repositories, service filters, and runtime continuation paths
- Reject cross-tenant access and replay attempts even when deployment refs or run IDs are guessed correctly
- Make tenant scope explicit in list and query APIs so operators can reason about access boundaries

### 6D. Governance Identity Surfaces

Expose the minimal external identity metadata required for trustworthy governance and review.

Requirements:

- Add submitter, approver, and policy-actor lineage to run and approval payloads where appropriate
- Include principal identity metadata in audit records without leaking secrets or token material
- Document the new identity and access semantics in phase-owned documentation and tests

## Acceptance Criteria

- Every external request reaches route logic with an authenticated principal or is rejected
- Approval resolution no longer trusts a request-body `approver` field as the source of truth
- Runs, approvals, and audits are tenant-scoped and reject cross-tenant access
- Identity lineage is visible in governance surfaces and recorded in audit
- Focused service, approval, and audit tests cover authn, authz, and tenancy boundaries

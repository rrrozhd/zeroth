# Console Semantic Port Design

**Date:** 2026-07-21

**Status:** Approved for planning

## Objective

Port the console rebuild from GitHub PR #4 (`feat/console-rebuild`, tip
`4d39d15abab3608322ca20ee55a99f95c46607c5`) onto the refactored Zeroth core
and preserve the complete user-facing feature set. The port includes the
admin-gated Regulus proxy required by the console's Regulus screens.

This is a semantic port, not a Git merge. The source PR is based on the public
pre-refactor history and cannot be merged safely into the refactored line.

## Source and Destination

- Source: GitHub PR #4 relative to its merge base
  `d574ca82082f1696cb9e6495200e7812830db4a6`.
- Destination: `codex/structured-token-release-blockers`, currently based on
  `codex/backend-architecture-refactor`.
- The source frontend behavior and presentation are authoritative.
- The destination package boundaries, runtime contracts, generated schemas,
  release metadata, and architecture guards are authoritative.

## Port Strategy

### Console application

Copy the source PR's console pages, components, primitives, hooks, styling,
tests, and public assets. Resolve the files that changed independently on the
refactored line by preserving the new-core API surface and adapting the console
callers to it. Do not overwrite refactor-era repository instructions.

The port must preserve the rebuilt navigation groups and screens:

- Operate: overview, runs, approvals, audit, and deployments.
- Build: Studio, templates, connectors, webhooks, and the existing React Flow
  editing behavior.
- Govern: cost, retention, rightsizing, and metrics.
- Regulus: dashboard, capabilities, enforcement, costing, and reconciliation.
- Learn: guide and associated navigation.

### Regulus service boundary

Reimplement the source PR's proxy through the refactored service layer rather
than copying the old `zeroth.core.service` files. The proxy must:

- require an admin-only `ECON_ADMIN` permission;
- mint the econ-plane credential in process without exposing the issuer;
- forward only the declared read-only GET allowlist and enforcement decision
  operations;
- reject authentication paths, path traversal, and unapproved methods;
- return a clear unavailable response when Regulus is not mounted; and
- remain behind the existing service authentication and dependency-injection
  boundaries.

Authorization changes must compose with the refactored role model. This port
does not import the unrelated custom-role and approval-notification feature from
PR #5.

### Generated contracts

Treat the source PR's `openapi.json`, generated TypeScript API types, and
Regulus schema as evidence of intended UI consumption, not artifacts to copy
unchanged. Generate fresh schemas and clients from the integrated refactored
backend, then reconcile only intentional differences.

The console must not retain references to routes, fields, or enum members that
do not exist in the generated new-core contracts.

### Release metadata

Do not copy PR #4's historical `0.10.1` through `0.10.6` version changes. Keep
the destination at `0.11.1` while the semantic port is developed. The default
structured-token flip and the coordinated `0.12.0` release remain a separate
final release step after this integration passes its gates.

Planning documents under `.planning/console-rebuild/` are preserved as design
history, but their old package paths and version statements are not release
authority for the refactored line.

## Data Flow

The browser continues to call the Zeroth service using the configured API base
and API key. Normal console requests use the regenerated typed client. Regulus
requests call the authenticated `/v1/econ/regulus/*` service surface. The proxy
authorizes `ECON_ADMIN`, obtains an internal econ-plane credential, validates
the requested operation against its allowlist, and forwards the request to the
mounted Regulus application.

No browser-visible econ administrative credential is introduced.

## Error Handling and Security

- Loading, empty, permission-denied, unavailable, and mutation-failure states
  remain explicit in the console.
- Regulus navigation remains hidden unless an authenticated admin can reach the
  proxy.
- Proxy transport and upstream failures are converted into stable service
  errors without leaking credentials or internal exception details.
- Path normalization is performed before allowlist matching.
- Existing tenant and service authentication behavior remains unchanged.

## Testing Strategy

Implementation follows test-driven development for adapted or new behavior.
Generated artifacts are verified by regeneration and clean-diff checks.

Required focused verification includes:

- console unit tests and production build;
- proxy authorization, allowlist, traversal, unavailable-mount, and forwarding
  tests against the refactored service assembly;
- OpenAPI and generated-client consistency checks;
- service route inventory and authorization tests;
- architecture, cold-import, and public-surface checks; and
- Python full-suite and Ruff checks.

After the semantic port passes, the separate default-ON release change must run
the entire suite with the implicit structured-token default, the explicit
legacy compatibility suite, all checker commands, and independent adversarial
review of the final release SHA.

## Commit Structure

Use normal atomic commits without bypassing hooks:

1. preserve the approved design and implementation plan;
2. transplant console-only source files;
3. add the refactored Regulus authorization and proxy with tests;
4. regenerate and reconcile API contracts;
5. resolve integration defects with focused regression tests; and
6. update documentation describing the new-core console integration.

## Acceptance Criteria

- Every intended PR #4 screen and navigation path exists on the refactored
  branch.
- The console builds from freshly generated new-core API contracts.
- Regulus screens use the secure refactored proxy and cannot bypass its
  authorization or allowlist.
- No pre-refactor backend module is restored or newly depended upon.
- PR #4's obsolete version metadata is not imported.
- Focused and full verification pass with a clean worktree at the integrated
  commit.

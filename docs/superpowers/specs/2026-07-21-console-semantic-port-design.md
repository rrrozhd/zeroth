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

After every acceptance gate passes, the destination tip replaces
`origin/main`. This is an intentional history replacement: the refactored line
and public main have unrelated roots, so a normal fast-forward or meaningful
merge is unavailable.

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

- introduce a reserved `ServiceRole.PLATFORM_ADMIN` authority and require its
  `ECON_ADMIN` permission; ordinary tenant-scoped `ServiceRole.ADMIN`
  principals never receive `ECON_ADMIN`;
- mint the econ-plane credential in process without exposing the issuer;
- forward only the exact route table declared below;
- reject authentication paths, path traversal, and unapproved methods;
- return a clear unavailable response when Regulus is not mounted; and
- remain behind the existing service authentication and dependency-injection
  boundaries.

Authorization changes must compose with the refactored role model. This port
does not import the unrelated custom-role and approval-notification feature from
PR #5.

`PLATFORM_ADMIN` is a reserved built-in role delivered only by a trusted static
credential configuration or verified bearer-token claim. It may use ordinary
admin console functions in its configured tenant, but it is the only role with
cross-tenant Regulus authority. Tests must prove that tenant admins from the
same and different tenants receive 403 for both Regulus reads and enforcement
decisions.

### Proxy route contract

The proxy uses an exact, segment-aware route table. It accepts these GET route
templates and no others:

- `dashboard/kpis`, `dashboard/top-creators`,
  `dashboard/capital-destroyers`, `dashboard/capability-ranking`,
  `dashboard/confidence-trend`, `dashboard/efficiency-trend`,
  `dashboard/calibration-trend`, `dashboard/action-suppression`,
  `dashboard/confidence-gate-status`, and `dashboard/data-quality-mix`;
- `dashboard/policy-timeline`, `dashboard/drift-timeline/{capability_id}`, and
  `dashboard/implementation-compare/{capability_id}`;
- `registry/capabilities`, `registry/capabilities/{capability_id}`, and
  `registry/implementations/{implementation_id}`;
- `evaluations/{capability_id}/latest` and
  `evaluations/{capability_id}/history`;
- `enforcement/actions` and `enforcement/policy-actions`;
- `costing/profiles/{profile_id}` and
  `costing/estimates/{capability_id}/latest`;
- `performance/summary`, `performance/capabilities`; and
- `reconciliation/calibration-summary`.

The only accepted POST templates are
`enforcement/actions/{positive_integer_action_id}/approve` and
`enforcement/actions/{positive_integer_action_id}/reject`. Their body is a
strict JSON object containing only `reason`, a string of at most 2,000
characters; the raw request body is capped at 8 KiB before parsing. Existing UI
GETs send no query parameters. The route table therefore declares no query
parameters initially, and the proxy rejects unexpected query keys instead of
forwarding them.

Before matching, the proxy inspects the raw path and the framework-decoded path,
rejects encoded or decoded dot segments, encoded path separators, backslashes,
absolute paths, repeated separators, NULs, and control characters, and then
matches complete path segments. The upstream client does not follow redirects.

### Generated contracts

Treat the source PR's `openapi.json`, generated TypeScript API types, and
Regulus schema as evidence of intended UI consumption, not artifacts to copy
unchanged. Generate fresh schemas and clients from the integrated refactored
backend, then reconcile only intentional differences.

Fix `scripts/dump_openapi.py` to construct the application from
`zeroth.service.app`; the legacy `zeroth.core.service.app` compatibility path is
not a generator dependency. Add a separate deterministic Regulus schema dump
from `zeroth.econ.plane.main` because mounted FastAPI sub-application routes do
not appear in the parent OpenAPI document. Frontend scripts generate
`openapi.json`/`api-types.ts` and
`openapi.regulus.json`/`api-types.regulus.ts` independently. CI runs both
generators in check mode and fails on any JSON or TypeScript diff.

The console must not retain references to routes, fields, or enum members that
do not exist in the generated new-core contracts.

### Release metadata

Do not copy PR #4's historical `0.10.1` through `0.10.6` version changes. Keep
the destination at `0.11.1` while the semantic port is developed. The default
structured-token flip and the coordinated `0.12.0` release remain a separate
final release step after this integration passes its gates.

The v0.12.0 effective default must not violate the immutable legacy callable
signature. `ExecutionSettings.sequential_join_enabled` retains its pinned
`bool = False` field declaration, but one canonical effective-mode helper treats
an unauthored/absent field as token mode, explicit `True` as token mode, and
explicit `False` as legacy mode. Every production consumer uses that helper;
the raw compatibility value is never interpreted as the effective default.
This preserves the legacy ABI and absent/true/false wire distinction while
making ordinary unauthored graphs run with legacy off.

Planning documents under `.planning/console-rebuild/` are preserved as design
history, but their old package paths and version statements are not release
authority for the refactored line.

## Data Flow

The browser continues to call the Zeroth service using the configured API base
and API key. Normal console requests use the regenerated typed client. Regulus
requests call the authenticated `/v1/econ/regulus/*` service surface. The proxy
authorizes the reserved platform role's `ECON_ADMIN`, obtains an internal
econ-plane credential, validates
the requested operation against its allowlist, and forwards the request to the
mounted Regulus application.

No browser-visible econ administrative credential is introduced.

## Error Handling and Security

- Loading, empty, permission-denied, unavailable, and mutation-failure states
  remain explicit in the console.
- Regulus navigation remains hidden unless an authenticated admin can reach the
  proxy; only `PLATFORM_ADMIN` can make that probe succeed.
- Tenant-scoped admins cannot read or mutate global Regulus state, regardless
  of whether their tenant matches another configured service resource.
- Proxy transport and upstream failures are converted into stable service
  errors without including exception text, request URLs, credentials, or
  internal response bodies.
- Path normalization is performed before allowlist matching.
- Missing internal credentials, disallowed redirects, invalid bodies, and
  unknown query parameters fail closed.
- Existing tenant and service authentication behavior remains unchanged.

## Testing Strategy

Implementation follows test-driven development for adapted or new behavior.
Generated artifacts are verified by regeneration and clean-diff checks.

Required focused verification includes:

- console unit tests and production build;
- a source-tip route and interaction inventory that records every PR #4 page,
  navigation entry, loading/empty/error state, polling flow, optimistic
  mutation, and Studio React Flow interaction before transplanting files;
- navigation and API-mock smoke tests for all route groups in addition to the
  existing focused unit tests;
- proxy platform-versus-tenant authorization, exact route table, unexpected
  query, decoded and encoded traversal/separator, redirect, body size/schema,
  missing credential, unavailable mount, sanitized upstream error, and
  forwarding tests against the refactored service assembly;
- deterministic main-app and Regulus OpenAPI generation plus clean-diff checks
  for both generated TypeScript clients;
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
2. capture the source interaction inventory, then transplant console-only
   source files while retaining the destination-only `/webhooks` route;
3. add the refactored Regulus authorization and proxy with tests;
4. regenerate and reconcile API contracts;
5. resolve integration defects with focused regression tests; and
6. update documentation describing the new-core console integration.

## Main Cutover and PR Cleanup

Before changing any remote ref, fetch `origin/main` and record its exact object
ID. Preserve that object ID on both a durable local branch and a pushed remote
backup branch named `codex/pre-refactor-main-20260721`. Preserve the PR #4 tip
locally until post-push verification completes.

Create the local backup from the recorded `<old-main-sha>` and verify its object
ID. Query the remote backup name before pushing: if it exists at any different
SHA, abort rather than overwrite it; if it is absent, create it with a lease
that requires absence; if it already equals `<old-main-sha>`, leave it intact.
Fetch the remote backup and verify both backup refs equal `<old-main-sha>` before
cutover.

After local verification finishes, record the immutable
`<tested-release-sha>`. Every cutover and post-cutover comparison uses that SHA,
not mutable `HEAD` or a branch name.

The cutover uses an explicit lease tied to the fetched old-main object ID:

```bash
git push --force-with-lease=refs/heads/main:<old-main-sha> \
  origin <tested-release-sha>:refs/heads/main
```

Raw `--force` is prohibited. If the lease fails, stop and re-evaluate the new
remote state rather than overwriting it. After the push, fetch `origin/main`
again and verify its object ID and tree exactly match the tested local tip.
Wait for every required main-triggered CI/status check and release smoke check
to pass for that exact fetched `<tested-release-sha>`. A successful ref update
alone is not permission to close or delete anything.

Only then clean up superseded collaboration state:

- immediately fetch `origin/feat/console-rebuild`, record
  `<console-pr-head-sha>`, verify GitHub still reports it as PR #4's head, and
  require it to equal the reviewed source tip
  `4d39d15abab3608322ca20ee55a99f95c46607c5`; if it differs, preserve the new
  tip and abort cleanup until its commits are reviewed and ported or explicitly
  declared superseded;
- only after that equality check, close PR #4 with a comment linking the
  replacement main commit and stating that its semantic behavior was ported and
  verified;
- delete remote `feat/console-rebuild` only after PR #4 is closed, its recorded
  tip is recoverable locally, and the deletion uses
  `--force-with-lease=refs/heads/feat/console-rebuild:<console-pr-head-sha>`;
- leave PR #5 open because custom roles and approval notifications are separate
  features not included in this port; and
- leave the historical merged PR #3 record intact. Its source branch is already
  absent, and GitHub PR records are not deleted.

The current main backup is not removed as part of this task. Rollback remains a
lease-protected push of that preserved tip if post-cutover verification reveals
a release-blocking problem:

```bash
git push --force-with-lease=refs/heads/main:<tested-release-sha> \
  origin <old-main-sha>:refs/heads/main
```

After rollback, fetch main and verify its object ID equals `<old-main-sha>`.

## Acceptance Criteria

- Every intended PR #4 screen and navigation path exists on the refactored
  branch, and the destination-only `/webhooks` page remains reachable.
- The source interaction inventory is represented by automated route,
  navigation, state, polling, mutation, and Studio behavior checks.
- The console builds from freshly generated new-core API contracts.
- Regulus screens use the secure refactored proxy and cannot bypass its
  authorization or allowlist.
- No pre-refactor backend module is restored or newly depended upon.
- PR #4's obsolete version metadata is not imported.
- Copied font imports have their required frontend dependencies and lockfile
  entries. The sidebar version is derived from generated project metadata, or a
  test proves it equals the Python package version; `0.10.6` is never hardcoded.
- Focused and full verification pass with a clean worktree at the integrated
  commit.
- The old main tip is recoverable from the documented local and remote backup
  branch, both backup refs are verified before cutover, the force-with-lease
  uses the recorded old-main SHA and immutable tested release SHA, and the
  fetched post-push main exactly matches the verified release commit.
- Required main CI and smoke checks pass on the exact replacement SHA before PR
  cleanup begins.
- PR #4 is closed as superseded only after main verification, and its remote
  head is first proven to equal the reviewed source tip; its remote branch is
  deleted only with a lease tied to that freshly verified PR head. Any newer PR
  head aborts cleanup. PR #5 and merged PR #3 are not incorrectly removed.

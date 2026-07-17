# Console Rebuild — Findings & Gap Analysis

Date: 2026-07-17 · zeroth-core v0.10.0.0.3

## What exists today (frontend/)

- **Stack**: Next.js 16 (static export, mounted at `/console`), React 19, Tailwind 4,
  React Flow (`@xyflow/react`), CodeMirror. Real typed API client.
- **~12.4k LOC** across `app/`. Pages present: Overview (`/`), Runs, Approvals, Audit,
  Studio (+ `studio/edit` graph canvas, 2.4k LOC), Cost, Connectors, Guide.
- **API layer**: `app/lib/api.ts` (646 LOC hand-written client) + `app/lib/api-types.ts`
  (4093 LOC, auto-generated from `openapi.json` via `npm run gen:api`).
- Quality is functional but visually generic; does **not** match the handoff design language.

## The handoff (design_handoff_zeroth_console/)

- `Zeroth Console.dc.html` (1067 lines) — single-file interactive prototype, all data mocked
  in one logic class. High-fidelity: colors/type/spacing/interaction states are final intent.
- 10 screens: Overview, Studio (3-pane graph canvas), Runs (master-detail), Approvals, Audit,
  Cost, Templates, Connectors (+ webhooks), Retention & Compliance, Guide.
- Design tokens fully specified (dark, teal accent `#5eead4`, IBM Plex Sans/Mono). README.md
  in the bundle is the design contract.

## The real API surface — bigger than either UI or handoff

Backend has **27 routers** (`src/zeroth/**/*_api.py` + `econ_plane/*/api.py`):

- **core/service**: studio, deployment, run, admin, approval, audit, cost, template,
  connector, webhook, manifest, contracts, artifact, **retention**, rightsizing, econ_analytics.
- **econ_plane**: statistics, reconciliation, connectors, capabilities, costing, auth,
  instrumentation, dashboard, enforcement, counterfactual, performance.

### Gap 1 — `frontend/openapi.json` is STALE
It has ~50 paths and is **missing `retention_api` routes** (and likely others). Must regenerate
via `gen:api` before we can claim to cover "all functions."

### Gap 2 — handoff screens missing from current frontend
- **Templates** (API `/v1/templates*` exists; no page today)
- **Retention & Compliance** (API `retention_api.py` exists; no page today)

### Gap 3 — API functions with NO screen in the handoff mocks
Handoff is design truth, not function truth. These real capabilities have no mock screen:
attestation / verify-attestation / evidence / timeline, input/output-contract &
result-error-state-schema, node-types, workflow diff, manifests, metrics, artifacts,
connector `test`, admin `interrupt`, model rightsizing, econ_analytics, and the entire
**econ_plane** subsystem (11 routers: enforcement, counterfactual, reconciliation, etc.).

**Implication of "implement all possible functions":** we extend the handoff's design
language to render screens/panels for API capabilities the mock never showed. The handoff
sets *how it looks*; the regenerated OpenAPI sets *what exists*.

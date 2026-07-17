# Zeroth Console Rebuild — Completion Summary

Branch: `feat/console-rebuild` · 28 commits · **v0.10.1 → v0.10.6** · nothing pushed.
Spec: [DESIGN.md](DESIGN.md) · Plans: [plans/](plans/) · Regulus: [REGULUS-FINDINGS.md](REGULUS-FINDINGS.md)

## What shipped

The entire `frontend/` visual/UX layer was replaced with a pixel-close
implementation of `design_handoff_zeroth_console/`, wired to the **real** API
(the handoff mocks were design-only). Delivered in 6 verified phases.

| Phase | Version | Screens / work |
|---|---|---|
| P0 Foundation | 0.10.1 | Design tokens, primitives, toast, polling, Regulus detection, Connect bar, app shell (sidebar/topbar/nav), **Overview** |
| P1 Operate | 0.10.2 | Runs, Approvals, Audit, Deployments |
| P2 Build | 0.10.3 | Studio (list + canvas re-skin), Templates, Connectors + webhooks |
| P3 Govern | 0.10.4 | Cost, Retention, Rightsizing & Efficiency, Metrics |
| P4 Regulus | 0.10.5 | **New admin-gated backend proxy** + Econ Dashboard, Capabilities, Enforcement, Costing, Reconciliation |
| P5 Polish | 0.10.6 | Guide, a11y basics, legacy-debt docs |

**~19 screens across 5 nav groups** (Operate · Build · Govern · Regulus · Learn),
all wired to live endpoints and verified in-browser against a running backend.

## Architecture

- **Kept** the typed API client (regenerated `api-types.ts` + `api.ts`), Connect
  bar, `runEligibility`. **Replaced** all styles, shell, and pages. Dark-only.
- New: design tokens (`globals.css`), primitives (`components/primitives/`),
  `useLoad`/`usePolling` hooks, `Toast`, `regulusApi` client, `+ ~15 page routes`.
- Studio canvas: **re-skinned in place** (React Flow + all workflow logic
  preserved), not rewritten.

## Notable engineering

- **Bugs caught by verification and fixed:** Regulus false-positive detection
  (auth-middleware 401 misread as "mounted"); rollback needs `target_graph_version`;
  approval resolution has no `note` field; audit sig is three-state; `/v1/metrics`
  returns Prometheus **text** not JSON; two `regulusApi` type aliases; enforcement
  `reason` must be `""` not `null`. The `regulus.base_url` fix also repaired Cost MTD.
- **Security (P4):** discovered the deep Regulus endpoints are *intentionally*
  console-unreachable (econ-Admin JWT, issuer HTTP-blocked; data is global, not
  tenant-scoped). Rather than bypass it, added an **admin-gated** (`ECON_ADMIN`
  permission) read-only + enforcement-approve/reject **proxy** (`/v1/econ/regulus/*`)
  that mints the econ token in-process. Backend security tests: operator→403,
  allowlist rejects auth/traversal paths.
- Every subagent refused to fabricate fields the real API lacks; empty/loading/error
  states on every screen; every mutation is optimistic + toasts.

## Verification

- `next build` (static export, ~22 routes) green throughout; `vitest` 12/12;
  backend `pytest` (proxy + app) 10/10. Each screen screenshotted vs. the handoff
  against a live seeded backend (regulus-enabled in P4).

## Known follow-ups (tracked in [plans/PLAN-INDEX.md](plans/PLAN-INDEX.md))

- Retire legacy `ui.tsx` + P0 compat aliases (still power the Studio-canvas kit; ~4k LOC).
- Content-type-aware `apiFetch` (removes the Metrics text double-fetch).
- Regulus registry/capability writes (deliberately out of the console).

## Not done (needs user action)

- **Nothing pushed** (repo policy = commit-on-ask). Push + PR when authorized.
- Running the console against a **production** backend requires setting the
  Connect bar (API base + key) and, for Regulus, `ZEROTH_REGULUS__ENABLED=true`
  + the `regulus` extra + an `admin`-role key.

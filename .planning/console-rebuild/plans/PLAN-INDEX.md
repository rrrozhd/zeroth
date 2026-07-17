# Zeroth Console Rebuild — Plan Index

Spec: [../DESIGN.md](../DESIGN.md) · Findings: [../FINDINGS.md](../FINDINGS.md)

Phased per the design. Each phase is its own plan file, produces working+verifiable
software, ends with one atomic commit + a version bump (repo policy). Later-phase
plans are written **just-in-time** after the prior phase lands, so they absorb what
we learn (ponytail: don't pre-write plans for work earlier phases will re-inform).

| Phase | Plan file | Status | Ships |
|---|---|---|---|
| **P0** Foundation | [P0-foundation.md](P0-foundation.md) | ✅ done (v0.10.1) | OpenAPI regen, design tokens, primitives, toast, polling, Connect bar, app shell, **Overview** |
| **P1** Operate | [P1-operate.md](P1-operate.md) | ✅ done (v0.10.2) | Runs, Approvals, Audit, Deployments |
| **P2** Build | [P2-build.md](P2-build.md) | ✅ done (v0.10.3) | Studio (canvas re-skin), Templates, Connectors + webhooks |
| **P3** Govern | [P3-govern.md](P3-govern.md) | ✅ done (v0.10.4) | Cost, Retention, Rightsizing & Efficiency, Metrics |
| **P4** Regulus | [../REGULUS-FINDINGS.md](../REGULUS-FINDINGS.md) | ✅ done (v0.10.5) | Econ Dashboard, Capabilities, Enforcement, Costing, Reconciliation — via a new admin-gated backend proxy (`/v1/econ/regulus/*`) |
| **P5** Guide & polish | [P5-polish.md](P5-polish.md) | ✅ done (v0.10.6) | Guide, a11y basics, rf-error triage, legacy-debt docs |

## Known follow-ups (post-rebuild)

- **Retire legacy `ui.tsx` + P0 compat aliases.** Still used by the Studio-canvas
  subtree (`studio/edit`, `StudioNodeView`, `NodeInspector`, `ConnectorInline`,
  `ModelRightsizing`, `CodeEditor`). The canvas is visually re-skinned (P2); this
  is ~4k LOC of internal cleanup, deferred as its own effort.
- **Content-type-aware `apiFetch`.** `/v1/metrics` (and any text endpoint) returns
  non-JSON; the Metrics page works around it with a text refetch. A shared
  content-type check in `apiFetch` would remove the double-fetch.
- **Regulus writes beyond enforcement** (registry/capability mutations) — left off
  the console deliberately; design separately if needed.

## Global rules (all phases)

- **Next.js 16 is not the Next you know.** Per `frontend/AGENTS.md`, read the
  relevant guide under `frontend/node_modules/next/dist/docs/` before writing any
  Next-specific code (routing, layouts, `use client`, metadata, static export).
- **Design truth** = `design_handoff_zeroth_console/` (README + `.dc.html`).
  **Function truth** = regenerated `openapi.json` (+ regulus). Reproduce the look;
  wire the real endpoints.
- **Keep** the typed API layer (`app/lib/api.ts`, generated `api-types.ts`),
  `config.ts`, `lastWorkflow.ts`, `runEligibility.ts`(+test). Replace all styles,
  shell, and page/component UI.
- Dark-only theme (handoff has no light mode). Remove `prefers-color-scheme`.
- Every mutating action → optimistic UI + one toast. Every screen handles
  loading / empty / error.
- Verify each phase **live** against the running backend via the preview tools
  before closing it; screenshot vs. the handoff.
- `vitest` green + `next build` succeeds at each phase end. Commit is atomic;
  bump `pyproject.toml` version (frontend-only phases are Low `x.x.x.x` unless the
  user says otherwise).

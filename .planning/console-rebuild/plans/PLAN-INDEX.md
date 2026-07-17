# Zeroth Console Rebuild — Plan Index

Spec: [../DESIGN.md](../DESIGN.md) · Findings: [../FINDINGS.md](../FINDINGS.md)

Phased per the design. Each phase is its own plan file, produces working+verifiable
software, ends with one atomic commit + a version bump (repo policy). Later-phase
plans are written **just-in-time** after the prior phase lands, so they absorb what
we learn (ponytail: don't pre-write plans for work earlier phases will re-inform).

| Phase | Plan file | Status | Ships |
|---|---|---|---|
| **P0** Foundation | [P0-foundation.md](P0-foundation.md) | ✅ done (v0.10.1) | OpenAPI regen, design tokens, primitives, toast, polling, Connect bar, app shell, **Overview** |
| **P1** Operate | _(write after P0)_ | pending | Runs, Approvals, Audit, Deployments |
| **P2** Build | _(write after P1)_ | pending | Studio (canvas re-skin), Templates, Connectors + webhooks |
| **P3** Govern | _(write after P2)_ | pending | Cost, Retention, Rightsizing & Efficiency, Metrics |
| **P4** Regulus | _(write after P3)_ | pending | Econ Dashboard, Capabilities, Enforcement, Costing, Reconciliation (graceful-degrade) |
| **P5** Guide & polish | _(write after P4)_ | pending | Guide, states polish, a11y basics, final sweep |

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

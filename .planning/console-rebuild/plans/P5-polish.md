# P5 Polish — Implementation Plan

> **For agentic workers:** superpowers:subagent-driven-development for the Guide screen; inline for the small fixes.

**Goal:** Close out the console — Guide screen, and targeted polish.

**Versioning:** intermediate commits bump Fix; phase-cap → Med `0.10.6`.

---

### Task 1: Guide — `app/guide/page.tsx` (re-skin, drop ui.tsx)

Handoff README §10. Rebuild fresh on P0 primitives:
- 3 concept cards (Graphs / Contracts / Governance — teal mono titles, 12.5px body).
- Node-type reference: 3-col grid of 6 cards (type-color square + mono name + one-liner), colors from `NODE_TYPE_COLOR`.
- API quickstart: dark `CodeBlock` with export + cURL (`POST /v1/runs`, `X-API-Key` redacted placeholder).
- Links out to hosted docs (real URLs from README if present; else the repo docs).
- Must no longer import `@/app/components/ui`. States trivial (static content). Verify tsc + build.

---

### Task 2: React Flow `nodeTypes`/`edgeTypes` memoization fix

`app/studio/edit/page.tsx` — `nodeTypes` is already module-level (line ~105). Find the object that IS recreated each render (likely an inline `edgeTypes`, `defaultEdgeOptions`, or a `nodeTypes`/`edgeTypes` passed as an inline literal) and hoist it to module scope or `useMemo` so the `rf-error 002` warning stops. Behavior unchanged. Verify no console rf-error after.

---

### Task 3: a11y basics sweep

- `aria-label` on icon-only / dot-only controls (sidebar Connect footer, filter chips, canvas palette buttons where unlabeled).
- Confirm one `<main>` landmark (AppShell has it) and that the sidebar is a `<nav>` (it is).
- Confirm focus-visible ring (global, P0) reaches all interactive controls; color is never the sole status signal (dot + text everywhere — already true).
- Light pass only — no ARIA gold-plating.

---

### Task 4: Accepted-debt documentation

The Studio-canvas subtree (`studio/edit`, `StudioNodeView`, `NodeInspector`, `ConnectorInline`, `ModelRightsizing`, `CodeEditor`) still uses the legacy `ui.tsx` kit + the P0 legacy-compat CSS aliases. The canvas was visually re-skinned to the handoff in P2; `ui.tsx` now only powers its inner form controls, which are functional and on-theme via the aliases. A full migration is ~4k LOC of high-risk churn for internal cleanup.
- Update the `globals.css` legacy-compat comment: these aliases are retained **solely for the Studio-canvas kit**, not "until end of P2".
- Note the full `ui.tsx` retirement as future work in the plan index.

---

### Task 5: Final sweep

- `npm test && npm run build` green.
- Full click-through of every screen against the live backend; `read_console_messages` clean (no rf-error).
- Med cap → `pyproject.toml` `0.10.6`, `version.ts` `0.10.6`, `uv lock`.
- Optional: `git status` clean of stray artifacts (econ_plane.db must stay out of the repo root — backend runs with `ECP_DATABASE_URL` in scratch).

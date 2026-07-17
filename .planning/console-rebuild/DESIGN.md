# Zeroth Console — Full Rebuild Design Spec

Date: 2026-07-17 · zeroth-core v0.10.0.0.3
Status: **DRAFT — awaiting user review**
Companion: [FINDINGS.md](FINDINGS.md) (gap analysis)

## 1. Goal

Replace the entire visual/UX layer of `frontend/` with a pixel-close
implementation of the `design_handoff_zeroth_console/` prototype, wired to the
**real** Zeroth API — covering *all* addressable functions (≈132 operations:
73 on the main console app + 59 on the mounted Regulus econ-plane), not just the
10 mocked handoff screens.

The handoff is the **source of truth for design**. The regenerated OpenAPI specs
are the **source of truth for function**. Where they diverge, we reproduce the
handoff's look and extend its design language to render capabilities the mock
never showed.

## 2. Locked decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Rebuild strategy | **Keep the typed API layer**, completely rebuild UI (shell, pages, components, styles). |
| Function scope | **Everything** — all reachable operations across both apps. |
| Delivery | **Phased** — each phase verified against the running backend. |
| Regulus (`/regulus`) | **Build it, degrade gracefully** — runtime-detect the mount; clean "not enabled" empty state when the `regulus` extra is absent. |
| Implementation planning | **superpowers writing-plans** (not GSD). |

## 3. Architecture

### 3.1 What we keep vs. replace
**Keep / regenerate:**
- Next.js 16 static export mounted at `/console`, React 19, Tailwind 4, React
  Flow (`@xyflow/react`), CodeMirror — the stack is right; don't churn it.
- The typed API contract: regenerate `app/lib/api-types.ts` from a fresh
  `openapi.json` (main app). Add `openapi.regulus.json` → `api-types.regulus.ts`
  for the Regulus surface.
- The Connect-bar pattern: API base URL + `X-API-Key` in `localStorage`
  (`app/lib/config.ts`), fetch wrapper in `app/lib/api.ts`.
- `app/studio/edit/runEligibility.ts` (+ its test) and the React Flow **canvas
  interaction logic** (drag, pointer-offset clamping, edge re-routing). Re-skin,
  don't rewrite.

**Replace entirely:**
- `app/globals.css` → new design-token system.
- `app/components/AppShell.tsx`, `ui.tsx`, and every page component.
- The mock live-run simulation → real ~2s polling of run timelines.

### 3.2 Data layer
- **`app/lib/api.ts`** stays the single fetch wrapper (base URL + key + JSON +
  error normalization + toast-on-error hook). Split into `app/lib/api/<domain>.ts`
  modules (runs, deployments, studio, connectors, webhooks, retention, econ,
  templates, cost, regulus) **only when** the flat file exceeds readability —
  ponytail: don't pre-split. Types come from generated `api-types.ts` (never
  hand-authored request/response shapes).
- **Polling**: a small `usePolling(fn, intervalMs, active)` hook (~15 lines,
  `setInterval` + cleanup + pause-when-hidden). Used for running runs (~2s),
  chain-verify progress, DLQ. No data-fetch library added — `fetch` + hook covers
  it (ponytail rung 4/5). Add SWR/React Query only if cache invalidation across
  screens becomes real pain.
- **Regulus detection**: on load, `GET /regulus/openapi.json` (or a cheap
  `/regulus/v1/...` probe) once; cache the boolean. Nav group + screens read it to
  choose live vs. "not enabled" state.

### 3.3 Static-export constraints
- All pages are client components (`"use client"`), data fetched at runtime — the
  export is a static shell; the FastAPI host serves it at `/console` and the app
  talks to `/v1/...` on the same origin (or a user-set base URL via Connect bar).
- Routing: keep Next App Router file routes, one dir per top-level screen.

## 4. Design system (from handoff README)

Reproduce exactly. Encode as CSS custom properties + Tailwind 4 `@theme` in
`globals.css`.

- **Fonts**: IBM Plex Sans (UI 400/500/600/700), IBM Plex Mono (identifiers, data,
  labels, code 400/500/600). Base font-size **13.5px**.
- **Backgrounds**: page `#0b0d11`; chrome/rails `#0d1015`; card `#11141a`;
  raised/canvas-node `#141822` / `#171b23`; code `#0d1015`.
- **Text**: primary `#e7eaf0`, secondary `#c6ccd8`, muted `#8f97a6`, faint
  `#5c6472`, disabled-dot `#3a4150`, code body `#a9b2c2`.
- **Accent teal**: `#5eead4`; tints `rgba(94,234,212,0.05–0.2)`; borders `.25–.6`.
- **Semantic**: success `#86efac`, warning `#fcd34d`, danger `#f87171`,
  info/exec `#93c5fd`, agent `#c4b5fd`, neutral `#a3adc2` (each + ~0.08 bg tint,
  ~0.3 border tint).
- **Node-type colors** (used canvas + timelines + guide): entrypoint `#5eead4`,
  agent `#c4b5fd`, exec `#93c5fd`, approval `#fcd34d`, retrieval `#86efac`,
  subgraph `#a3adc2`.
- **Borders**: hairline `rgba(255,255,255,0.06–0.08)`; controls `.1`.
- **Radii**: cards 8, buttons/inputs 6, chips 4–5, node-type squares 2.
- **Type scale**: page title 20/600; section label mono 10–10.5 uppercase
  ls .08–.12em; body 12.5–13.5; data mono 11–12.5; big stat mono 26/600.
- **Spacing**: page pad 26–28px; card pad 13–18px; grid gaps 10–12px; content
  max-width 1160px.
- **Keyframes**: `zpulse` (opacity 1→.35, 1.4–2.4s, live dots); `zfade`
  (page/toast entry, opacity 0→1 + translateY 4→0, .25s).
- **Selection** `rgba(94,234,212,0.25)`; scrollbars `#242a35` thumb.

**Logo**: use real SVGs at `docs/assets/logo/` (`zeroth-mark.svg` + wordmark),
copied into `frontend/public/`, not the typographic placeholder.

## 5. App shell

- **Sidebar** (212px, `#0d1015`, right hairline): logo + `zeroth/core` mono; below,
  real version (`v0.10.0.0.3 · console`). Nav grouped under mono-uppercase headings
  (see §6). Active item: teal tint bg + teal text/dot. Approvals shows live
  pending-count badge (amber). Footer: pulsing green dot + `{host}` + masked key
  from Connect-bar state.
- **Topbar** (52px): breadcrumb `{tenant} / {page}`; right: env badge (local
  gray / staging amber / production red) + "served: {deployment}@{version}" chip.
  Env + tenant come from Connect-bar config / deployment metadata.
- **Content scroller**: `flex:1; overflow-y:auto`, pages `zfade` in.
- **Toast**: fixed bottom-right, teal-bordered, auto-dismiss ~3.2s; every mutating
  action fires one (optimistic UI). Single global toast context.
- **Connect bar**: modal/inline editor for base URL + `X-API-Key`, persisted to
  `localStorage`; masked display in sidebar footer. Replaces handoff's static
  `127.0.0.1:8000 / demo-operator-••••`.

## 6. Information architecture

Extends the handoff's nav groups to hold every function. `*` = new vs. handoff.

- **Operate** — Overview · Runs · Approvals · Audit · **Deployments\***
- **Build** — Studio (canvas + node-types + contracts + diff) · Templates ·
  Connectors (memory connectors + webhooks/DLQ)
- **Govern** — Cost · Retention · **Rightsizing & Efficiency\*** · **Metrics\***
- **Regulus\*** *(gated)* — Econ Dashboard · Capabilities · Enforcement · Costing ·
  Reconciliation
- **Learn** — Guide

Deployments is promoted from a card to a screen because attestation, evidence,
input/output contracts, timeline, per-deployment audits, and verify-attestation
are too rich for the Overview card. The Overview card stays (summary + rollback)
and deep-links into the Deployments screen.

## 7. Screen specs

Each screen reproduces the handoff look (§ handoff README) and wires the endpoints
in the mapping table (§11). Highlights of what's added beyond the mock:

1. **Overview** — health tiles from `/health*` + `/v1/metrics`; deployments card
   from `/v1/deployments` (+ rollback); recent runs from `/v1/admin/runs`;
   getting-started checklist (audit-verify item completes live).
2. **Runs** (master-detail) — list `/v1/admin/runs`; detail `/v1/runs/{id}` +
   `/timeline` (poll ~2s) + `/evidence`; actions cancel/interrupt/replay;
   verify-chain + audit-verification; artifact drill-down `/v1/artifacts/{id}`;
   invoke-cURL block. Interrupt is new vs. handoff.
3. **Approvals** — cards from `/v1/deployments/{ref}/approvals`; resolve
   (approve/reject) → `/resolve`; decided cards show the recorded decision.
4. **Audit** — events from `/v1/deployments/{ref}/audits`; "Verify chain" →
   `/audit-verification` (deployment) and per-run `/verify-chain`; chip states
   idle→verifying→intact; color by event kind.
5. **Deployments\*** (master-detail) — list/create `/v1/deployments`; detail tabs:
   metadata, input/output contracts + result-error-state-schema, attestation
   (+ verify GET/POST), evidence, timeline, cost, audits, rollback.
6. **Studio** (three-pane) — graph list/CRUD `/api/studio/v1/workflows`; canvas
   from `GET {id}`, save via `PUT`; palette from `/node-types`; contracts panel
   `/contracts`; publish/clone; **diff** view (`/diff`) for draft-vs-published;
   Deploy → `POST /v1/deployments`; Run → `POST /v1/runs` then jump to Runs.
   Read-only banner for published/deployed; clone-to-edit.
7. **Templates** (master-detail) — `/v1/templates` list/create; detail `/{name}`;
   delete version `/{name}/{version}`; Jinja2 body + variable chips.
8. **Connectors** — memory connectors `/v1/connectors` CRUD + **test**
   (`/{ref}/test`); webhooks `/v1/webhooks/subscriptions` CRUD + dead-letters list
   + replay.
9. **Cost** — MTD spend `/v1/tenants/{id}/cost`; budget cap `PUT .../budget`;
   per-run ceiling (config note); spend-by-deployment `/v1/deployments/{ref}/cost`;
   top nodes by attributed cost (from run evidence/cost).
10. **Retention** — policy `GET/PUT /v1/retention/policy` (TTL rows editable);
    legal holds place/release; erasure requests `POST /erasure-requests` (execute →
    ERASED + chain-integrity toast).
11. **Rightsizing & Efficiency\*** — opportunities `/v1/econ/rightsizing/opportunities`;
    suggest `/rightsizing`; experiment `/rightsizing/experiment`; unit-economics;
    waste; attach quality-verdict. (Reuses/absorbs existing `ModelRightsizing.tsx`.)
12. **Metrics\*** — `/v1/metrics` + manifests `/v1/manifests` (small observability
    panel; may fold into Overview if thin).
13. **Regulus\*** (gated) — Econ Dashboard (14 `/regulus/.../dashboard/*` KPIs &
    trends), Capabilities registry (13 ops), Enforcement (7 — actions +
    approve/reject), Costing (4), Reconciliation (2). Absent-extra → empty state.
14. **Guide** — concept cards, node-type reference, API quickstart, docs links.

## 8. Cross-cutting behavior

- **States**: every screen handles loading (skeleton in card frames), empty
  ("nothing yet" + primary action), and error (inline red-tinted card + retry).
  The handoff only shows the happy path; real API needs these.
- **Optimistic mutations + toast** for every write (publish, deploy, run, cancel,
  interrupt, replay, approve, reject, verify, set-cap, replay-DLQ, erasure,
  delete-template, connector-test, rollback, legal-hold).
- **Auth**: all requests carry `X-API-Key`; 401/403 → toast + Connect-bar prompt.
- **Accessibility basics**: focus rings on controls, keyboard-operable nav/buttons,
  `aria-label` on icon-only controls, color never the sole status signal (dot +
  text). Not a full a11y pass — basics only (ponytail: don't gold-plate).

## 9. Testing & verification

- **Unit (vitest)**: keep `runEligibility.test.ts`; add focused tests only for
  non-trivial pure logic (status→color/label maps, cost math, polling hook
  transitions, diff rendering). No component-render test farm (ponytail).
- **Live verification per phase**: run the FastAPI backend (`uv run`) + `next dev`
  via the preview tools; drive each screen against the real API; screenshot the
  result vs. the handoff before closing the phase.
- Each phase ends green (`vitest`, `ruff` untouched) with one atomic commit and a
  version bump per repo policy.

## 10. Phase breakdown

| Phase | Ships | Verifies |
|---|---|---|
| **P0 Foundation** | Regen `openapi.json` (+regulus), tokens/`globals.css`, Tailwind `@theme`, app shell (sidebar/topbar/toast/Connect bar), routing skeleton, **Overview** end-to-end. | Shell + Overview render pixel-close against live `/health`, `/deployments`, `/metrics`. |
| **P1 Operate** | Runs, Approvals, Audit, Deployments. | Run polling advances; approve resumes; chain verify; attestation/evidence load. |
| **P2 Build** | Studio (canvas re-skin + publish/clone/deploy/diff/node-types/contracts), Templates, Connectors (+ test, webhooks/DLQ). | Draft→publish→deploy→run loop; template CRUD; webhook replay. |
| **P3 Govern** | Cost, Retention, Rightsizing & Efficiency, Metrics. | Set cap enforces; erasure flips to ERASED; rightsizing opportunities list. |
| **P4 Regulus** | Econ Dashboard, Capabilities, Enforcement, Costing, Reconciliation + graceful-degrade detection. | With extra: dashboards populate, enforcement approve/reject. Without: clean empty state. |
| **P5 Guide & polish** | Guide, empty/error/loading polish, a11y basics, final verification sweep. | Full-console walkthrough vs. handoff. |

## 11. Endpoint → screen coverage (proves "all functions")

**Main app (73):**
- `/health`, `/health/live`, `/health/ready` → Overview
- `studio` (11: contracts×2, node-types, workflows CRUD, clone, diff, publish) → Studio
- `admin/runs` (list, cancel, interrupt, replay) → Runs
- `artifacts/{id}` → Runs (evidence drill-down)
- `connectors` (list, create, update, delete, test) → Connectors
- `deployments` (list, create, approvals×3, attestation, attestation/verify,
  audit-verification, audits, cost, evidence, input/output-contract,
  result-error-state-schema, metadata, rollback, timeline, verify-attestation)
  → Deployments (+ Overview card, Approvals, Audit, Cost)
- `econ` (quality-verdict, rightsizing, rightsizing/experiment,
  rightsizing/opportunities, unit-economics, waste) → Rightsizing & Efficiency
- `manifests`, `metrics` → Metrics / Overview
- `retention` (policy get/put, legal-holds place/release, erasure-requests) → Retention
- `runs` (create, get, audit-verification, evidence, timeline, verify-chain)
  → Runs (create also from Studio)
- `templates` (list, create, get, delete) → Templates
- `tenants/{id}` (budget put, cost get) → Cost
- `webhooks` (dead-letters list, dl replay, subscriptions CRUD) → Connectors

**Regulus (59):**
- `auth` (2) → console self-auth / identity (`/auth/me`)
- `capabilities` (13: registry, implementations, evaluations) → Capabilities
- `dashboard` (14: KPIs, trends, rankings, drift) → Econ Dashboard
- `enforcement` (7: actions, approve, reject, policy-actions) → Enforcement
- `costing` (4: profiles, pricing-catalog, estimates) → Costing
- `connectors` (7) + `instrumentation` (4) → Costing/Dashboard side-panels
- `counterfactual` (4: outcomes) + `performance` (2) → Econ Dashboard
- `reconciliation` (2: calibration, ground-truth-import) → Reconciliation
- `statistics` (0) → n/a

## 12. Risks / open items

- **Regulus schema**: needs `uv sync --extra regulus` to dump its OpenAPI +
  generate types (P4 prerequisite). Base install lacks `email-validator`.
- **Contract/attestation payload shapes** are rich; some detail panels may render
  raw JSON first, then get bespoke layouts if a screen warrants it (ponytail).
- **`studio/edit` canvas** is 2.4k LOC — re-skin carefully to avoid regressing
  drag/edge logic that already has real workflow-API wiring.
- **Version label**: use real `pyproject.toml` version, not handoff's `v0.9.2`.

## 13. File plan (high level)

- Delete/replace: all of `app/*/page.tsx`, `app/components/*`, `app/globals.css`.
- Keep: `app/lib/config.ts`, `app/lib/lastWorkflow.ts`, `runEligibility.ts`(+test),
  regenerate `api-types.ts`, extend `api.ts`.
- Add: `app/lib/api/*` (as needed), `app/lib/regulus.ts`, `app/hooks/usePolling.ts`,
  `app/components/shell/*`, `app/components/primitives/*` (Card, Pill, StatusDot,
  Toast, CodeBlock, Table, MonoLabel…), `app/deployments/`, `app/templates/`,
  `app/retention/`, `app/rightsizing/`, `app/metrics/`, `app/regulus/*`.

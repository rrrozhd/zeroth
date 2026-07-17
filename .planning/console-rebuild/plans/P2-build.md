# P2 Build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Build the three Build screens — Templates, Connectors (+ webhooks/DLQ), and Studio (graph list + canvas) — on the P0 shell, wired to the live API, replacing/re-skinning the legacy pages.

**Architecture:** Templates + Connectors are fresh master-detail screens using the P0 primitives (like P1). **Studio is a re-skin, not a rewrite**: the existing `studio/edit/page.tsx` (2403 LOC) already wires React Flow + the full workflow API (publish/clone/deploy/diff/node-types) and `runEligibility` — keep that logic; restyle the node card, edges, canvas chrome, toolbar, read-only banner, config panel, and left rail to handoff §2. Retire `app/components/ui.tsx` per-page as screens stop importing it; remove the P0 legacy-compat CSS aliases once nothing uses them.

**Tech Stack:** Next 16 (static export), React 19, Tailwind 4, `@xyflow/react` (React Flow), CodeMirror, existing `apiFetch` client, vitest.

**Backend for verification:** same as P1 (service on `:8000`, key `demo-operator-secret-p1`, dev console on `:3000`). The seeded `demo-hello@1` graph is deployed; Studio can list/open it, clone to a draft, and edit.

**Versioning:** intermediate commits bump Fix (`0.10.2.0.1, …`); phase-cap commit bumps Med → `0.10.3`.

**Global rules:** dark-only; mutation → optimistic UI + toast; loading/empty/error each screen; API key only in localStorage. Read `frontend/node_modules/next/dist/docs/` before Next-specific code. Studio: preserve the React Flow interaction + API behavior; change styling only.

---

### Task 0: Template + webhook api wrappers

**Files:** Modify `app/lib/api.ts`.

- [ ] Add typed wrappers (read `api-types.ts` for exact schema names — likely `TemplateSummaryResponse`, `TemplateDetailResponse`, `WebhookSubscriptionResponse`, `WebhookSubscriptionCreateRequest`, `DeadLetterResponse`; confirm before use, no `any`):
```ts
// Templates
export type TemplateSummary = S["TemplateSummaryResponse"];   // confirm names
export type TemplateDetail = S["TemplateDetailResponse"];
export function listTemplates(): Promise<TemplateSummary[]> { return apiFetch("/v1/templates"); }
export function getTemplate(name: string): Promise<TemplateDetail> {
  return apiFetch(`/v1/templates/${encodeURIComponent(name)}`);
}
export function createTemplate(body: S["TemplateCreateRequest"]): Promise<TemplateDetail> {
  return apiFetch("/v1/templates", { method: "POST", body: JSON.stringify(body) });
}
export function deleteTemplateVersion(name: string, version: string): Promise<void> {
  return apiFetch(`/v1/templates/${encodeURIComponent(name)}/${encodeURIComponent(version)}`, { method: "DELETE" });
}
// Webhooks
export type WebhookSubscription = S["WebhookSubscriptionResponse"];
export type DeadLetter = S["DeadLetterResponse"];
export function listWebhookSubscriptions(): Promise<WebhookSubscription[]> { return apiFetch("/v1/webhooks/subscriptions"); }
export function createWebhookSubscription(body: S["WebhookSubscriptionCreateRequest"]): Promise<WebhookSubscription> {
  return apiFetch("/v1/webhooks/subscriptions", { method: "POST", body: JSON.stringify(body) });
}
export function deleteWebhookSubscription(id: string): Promise<void> {
  return apiFetch(`/v1/webhooks/subscriptions/${encodeURIComponent(id)}`, { method: "DELETE" });
}
export function listDeadLetters(): Promise<DeadLetter[]> { return apiFetch("/v1/webhooks/dead-letters"); }
export function replayDeadLetter(id: string): Promise<void> {
  return apiFetch(`/v1/webhooks/dead-letters/${encodeURIComponent(id)}/replay`, { method: "POST" });
}
```
Note whether list endpoints return a bare array or a `{items,total}` envelope (match the runs/connectors precedent). Verify `npx tsc --noEmit` → 0. Don't commit.

---

### Task 1: Templates — `app/templates/page.tsx` (replace P0 stub)

Handoff README §7. Master-detail:
- [ ] Left rail (280px): template rows (mono name + `vN` teal), selected = 2px teal left edge + tint. From `listTemplates()`.
- [ ] Detail: `name@vN` mono 17px; **Delete version** (danger → `deleteTemplateVersion(name, version)` + toast + refetch); variable chips (`{{ question }}` violet, mono 11px — parse from the template body/variables field); Jinja2 body in a dark `CodeBlock` (12.5px, line-height 1.75). Note on secret redaction in audit records.
- [ ] "New template" minimal form (name + version + body) → `createTemplate` if the request shape is simple; else defer with a note.
- [ ] States (loading/empty "No templates"/error). Verify tsc + build. Don't commit.

---

### Task 2: Connectors + webhooks — `app/connectors/page.tsx` (replace legacy)

Handoff README §8. Rebuild fresh with P0 primitives (drops the legacy `ui.tsx`/`ConnectorInline` dependency for this page):
- [ ] **Memory connectors** table from `listConnectors()`: name (mono) · type (thread-scoped/vector/key-value/run-ephemeral) · backend (postgres/pgvector/redis/in-memory) · attached-to · status (green mono). **Test** button per row → `testConnector(ref)` + toast (green ok / red fail). Create (`createConnector`) + edit (`updateConnector`) + delete (`deleteConnector`) — inline form/modal using the real `ConnectorCreateRequest` shape (read it).
- [ ] **Webhooks card**: subscriptions from `listWebhookSubscriptions()` — event chip (violet mono) + URL (mono, ellipsized) + status: `delivering ✓` (green) or `N dead-lettered` (amber, from `listDeadLetters()` grouped by subscription) with **Replay** → `replayDeadLetter(id)` (clears DLQ + toast). Add/delete subscription.
- [ ] States + verify tsc/build. Don't commit.

---

### Task 3: Studio list — `app/studio/page.tsx` (re-skin)

Handoff README §2 (left rail). Keep the existing data wiring (`listWorkflows`, `createWorkflow`, templates); restyle:
- [ ] Graph list: mono name + uppercase state colored by lifecycle (deployed `--accent`, published `--success`, draft `--text-muted`); selected row teal tint + border. Row click → open canvas (`/studio/edit?...` as today).
- [ ] "Start from a template" section: Grounded Q&A (RAG), Approval-gated action, Tool → Agent pipeline → instantiates an editable draft (existing `templates.ts` logic).
- [ ] Replace `ui.tsx`/`nodeMeta` styling with P0 primitives + `NODE_TYPE_COLOR`. Verify tsc/build. Don't commit.

---

### Task 4: Studio canvas — re-skin `studio/edit` (KEEP logic)

Handoff README §2. The single hardest task. **Read** `app/studio/edit/page.tsx`, `app/components/StudioNodeView.tsx`, `app/components/NodeInspector.tsx`, `app/components/nodeMeta.tsx` first. Preserve ALL React Flow wiring, state, drag, publish/clone/deploy/diff/run handlers, and `runEligibility`. Restyle only:
- [ ] **Node card** (`StudioNodeView`): 178px, bg `--bg-raised` (#141822), radius 8, shadow; type row (7px `NODE_TYPE_COLOR` square + mono 9px uppercase type), mono 12.5/600 name, 10.5px `--text-faint` sub (ellipsized); selected border `rgba(94,234,212,0.6)`, else `rgba(255,255,255,0.1)`.
- [ ] **Edges**: data edges solid `#3f4757`; agent→tool edges dashed `#c4b5fd` `4 4`, 1.5px, opacity .75 (React Flow custom edge styles / `edgeTypes`).
- [ ] **Canvas**: dot-grid background (`Background` variant dots, color `rgba(255,255,255,0.06)`, gap 22) on `--bg-page`.
- [ ] **Toolbar** (52px): graph name (mono 600) + state Pill + version; right side lifecycle-dependent — draft → node palette (colored 7px square + mono type shorthand agent/exec/gate/rag/sub) + **Publish** (primary); published → **Clone to draft** + **Deploy**; deployed → **Clone to draft** + **▸ Run**.
- [ ] **Read-only banner** (published/deployed): amber tint strip, mono 11.5px "read-only — published graphs are immutable; clone to a draft to edit".
- [ ] **Config panel** (`NodeInspector`, 280px right): type header, editable name input (disabled + "clone to edit" when read-only), key/value config fields (mono label + bordered value box), **Declared capabilities** chips (green tint; "none — pure transform" when empty), fail-closed enforcement note. Re-skin its `ui.tsx` usage to primitives/tokens; keep `ConnectorInline`/`ModelRightsizing` behavior (restyle if quick, else leave functional).
- [ ] Behavior unchanged: Publish→published/v1; Deploy→restart-required toast; Run→create run + jump to `/runs?run=<id>`; Clone→`-draft` copy selected.
- [ ] Verify tsc + build. Don't commit.

---

### Task 5: Cleanup + verify + phase cap

- [ ] Grep for remaining `ui.tsx` importers. Anything still using it (e.g. cost/guide until P3) keeps it; if nothing outside P3 screens uses it, note it for P3 removal. If ALL legacy token classes (`bg-surface`/`border-border`/…) are gone, remove the P0 legacy-compat aliases from `globals.css` and rebuild to confirm.
- [ ] Live verify (backend + preview): Templates CRUD; Connectors test + webhook replay; Studio open `demo-hello`, clone to draft, edit a node (config panel), publish, deploy (toast), run (→ Runs). Screenshot each vs. handoff. `read_console_messages` clean.
- [ ] `npm test && npm run build` green. Med cap → `pyproject.toml` `0.10.3`, `version.ts` VERSION `0.10.3`.
```bash
git commit -m "feat(console): v0.10.3 — Build screens (Studio re-skin, Templates, Connectors + webhooks)"
```

---

## Self-review checklist
- DESIGN §11 Studio (11 ops), Templates (4), Connectors (5) + webhooks (5) all mapped ✓
- Studio logic preserved, styling-only changes ✓ · publish→deploy→run loop verified (Task 5) ✓
- No `any` in api additions · loading/empty/error each screen ✓ · legacy `ui.tsx`/compat-aliases retirement tracked ✓

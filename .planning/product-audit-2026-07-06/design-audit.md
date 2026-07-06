# Zeroth Console — UI/UX Design Audit

Date: 2026-07-06 · Scope: `frontend/` (Next.js static-export console) · Branch: `feat/console-frontend`

---

## 1. App Map

### Routes (`frontend/app/`)
| Route | File | Purpose |
|---|---|---|
| `/` | `app/page.tsx` (129 L) | Overview: deployment hero card + quick-link grid |
| `/runs` | `app/runs/page.tsx` (234 L) | Submit run (JSON form), run list, run detail w/ polling + timeline |
| `/approvals` | `app/approvals/page.tsx` (137 L) | Pending approval cards (approve/reject) + resolved list |
| `/audit` | `app/audit/page.tsx` (89 L) | Per-node audit records as expandable `<details>` rows |
| `/cost` | `app/cost/page.tsx` (49 L) | Single aggregate spend number |
| `/studio` | `app/studio/page.tsx` (137 L) | Workflow list + create/delete |
| `/studio/edit?id=…` | `app/studio/edit/page.tsx` (459 L) | React Flow canvas editor, palette sidebar, node-editor modal |

### Major components
- `app/components/AppShell.tsx` — sticky top nav (6 links), DeploymentChip (health ping), ConnectPopover (base URL + API key → localStorage).
- `app/components/ui.tsx` (324 L) — design system: `PageHeader`, `Card`, `StatusBadge` (26 status→tone mappings), `Button` (4 variants × 2 sizes), `Input`/`Textarea`/`Field`, `Json`, `Mono`, `Empty`, `NotConnected`, `ApiErrorNote`, `useAsync`/`useConnected` hooks.
- `app/components/StudioNodeView.tsx` — canvas node card w/ typed port handles (control=amber, data=blue).
- `app/components/NodeInspector.tsx` — per-node-type config forms (`FIELD_SPECS` for 5 node types).
- `app/components/nodeMeta.tsx` — node-type blurbs + inline SVG glyphs.

### Design tokens (`app/globals.css`)
- Light/dark via `prefers-color-scheme` only (no manual toggle). Tokens: `--app-bg`, `--surface`, `--foreground`, `--muted` (zinc-600, ~7:1 AA), `--border`, `--accent` (indigo-600/500), `--ring`.
- Global `*:focus-visible` ring — good, consistent.
- React Flow chrome themed via `--xy-*` vars; minimap bordered; controls get focus ring.
- System font stack (deliberate, offline static export). No display font, no tailwind.config (Tailwind v4 `@theme inline`).

---

## 2. Screenshot Critique (repo root `verify-*.png`)

**Caveat:** the run-failed / run-paused screenshots predate the current `ui.tsx` — "Terminated by policy" and "Paused for approval" render as **gray** badges there, while current `STATUS_TONES` maps them red/amber with friendly labels ("Blocked by policy", "Awaiting approval"). `verify-audit-fixed.png` reflects current code (blue "Approval resolved"). Re-capture screenshots after next change.

- **verify-approvals.png** — Clean card, clear Approve (filled indigo) / Reject (outline red) pairing, amber "Pending" pill reads correctly. Weaknesses: the 32-char run hex is dumped raw with no truncation, copy affordance, or link to `/runs?run_id=…`; the `Node`/`Run` dt labels are very low-contrast (`text-zinc-400` ≈ 2.5:1 — fails AA); "Proposed payload" disclosure is a bare browser `<details>` triangle, visually orphaned; huge dead whitespace below one card — page feels unfinished when there's a single item.
- **verify-audit-fixed / verify-audit-trail.png** — Rows scan well (node id bold, run hex mono muted, badge right-aligned). Weaknesses: **no timestamps on collapsed rows**, so order/duration is invisible; the same run hex repeats in every row (no grouping by run); collapsed rows give **zero affordance that they expand** (default `<details>` marker is suppressed by `display:flex` on the summary); ISO timestamps shown raw (`2026-06-29T19:00:03.893201Z`); expanded panel shows only Audit ID/Started/Completed + output snapshot — the newly populated `tool_calls`, `token_usage`, `memory_interactions`, `stdout/stderr` fields (all present in `NodeAuditRecord`, api-types.ts:1231-1310+) are never rendered.
- **verify-run-failed.png** — Status semantics broken in this capture (gray badge for a policy kill; the red "Failure" heading carries all the alarm). Failure body is a raw JSON dump — `reason: policy_violation / capability denied: network_write` deserves a designed callout (icon, human sentence, details collapsed). Timeline card is a dead "Not loaded." + manual "Load timeline" button — feels like a debug page.
- **verify-run-paused.png** — Same: gray badge (pre-fix), amber "Awaiting approval" heading over a raw JSON blob containing an `approval_id` that is **not a link to /approvals** — the single most obvious action (go resolve it) requires manual navigation.
- **verify-run-succeeded.png** — Output is raw JSON with escaped `\n` markdown inside a string — illegible for the actual product output. Overall page hierarchy (mono run-id chip → status row → output → timeline) is sound.
- **Global**: consistent header, nice deployment chip, tidy typography scale. But every page is header + flat card stack; density is low, tables/lists carry no timestamps, and JSON is the primary data surface. Reads as a **well-groomed internal tool / late prototype**, not yet a production console.

---

## 3. Dimension Assessment

### Information architecture & navigation — B-
Flat 6-item nav maps 1:1 to backend resources — legible. But: Overview duplicates the nav as quick links instead of surfacing live state (pending approvals count, recent runs, spend); run selection state lives only in React state (`runs/page.tsx:41-45` reads `?run_id` on mount but never writes it back → no deep-linking after click, back button doesn't work); audit is not filterable by run and runs don't link to their audit records — the three views (run, audit, approval) describing the same execution are disconnected islands.

### Visual hierarchy & typography — B
Consistent scale (2xl page title / base card title / sm body / xs meta), good use of mono for ids. Weak points: raw ISO timestamps and full-length hexes create visual noise; card titles and section labels compete when both bold; the runs page puts the submit form above the list (authoring above monitoring — inverted priority for an ops console).

### Component system consistency — B-
`ui.tsx` is a genuine mini design system with thoughtful touches (disabled-primary contrast fix at ui.tsx:196-199, code-aware `ApiErrorNote`). Leaks: `NodeInspector.tsx:87-88` re-declares its own input class instead of the shared `fieldInput` (drifted: missing placeholder color); `approvals/page.tsx:95-108` uses raw `text-zinc-400/500/600` instead of the `text-muted` token; run-list rows use `hover:opacity-80` (runs/page.tsx:83) while every other hover uses bg tint; the "required" asterisk is amber (NodeInspector.tsx:109) — amber elsewhere means "pending/warning".

### State coverage — C+
Empty (`Empty`, `NotConnected`) and error (`ApiErrorNote`) are covered well. **Loading is not**: `useAsync` exposes `loading` but pages render *nothing* under the header while loading (only the Refresh button says "Loading…") — no skeletons, no spinner. Long-content overflow: `Json` (ui.tsx:247) has `overflow-auto` but **no max-height** — a large output snapshot makes the page arbitrarily tall. No unsaved-changes guard in the studio editor. Delete confirm is `window.confirm` (studio/page.tsx:43).

### Status color semantics — B+ (in current code)
26-status mapping with 4 hues (emerald/blue/amber/red) + zinc fallback is coherent and the dot+label pill is a good pattern. Two issues: unknown statuses silently fall back to zinc (as the stale screenshots show, a *policy termination rendered neutral gray* — dangerous failure mode; consider logging/defaulting unknown terminal-ish strings more loudly); `approval_api` → blue "Approval resolved" reads as informational while sitting next to emerald "Completed" for the same node — ambiguous.

### Accessibility — B-
Good: skip link (layout.tsx:20-25), global focus-visible ring, `aria-current` on nav, `aria-expanded/haspopup` on Connect, Escape handling, port handles have `aria-label`. Gaps: `text-zinc-400` labels fail contrast (approvals); NodeEditorDialog has **no focus trap and doesn't move focus into the dialog** on open (studio/edit/page.tsx:384-459) — keyboard users are left behind the overlay; `✕` close is a text glyph (has aria-label, ok); StatusBadge conveys state by color+text (ok); expandable audit rows lack any visual/ARIA expansion cue; canvas editing is mouse-only (no keyboard node add/connect — acceptable for v1, note it).

### Credibility verdict — "polished prototype"
The chrome (nav, tokens, badges, focus rings) is production-grade. The content layer is not: raw JSON as primary output surface, raw ISO timestamps, unlinked hex ids, no loading states, no timestamps in lists, dead "Load timeline" affordance. It looks like a credible **beta admin tool**; it does not yet look like a product a customer pays for.

---

## 4a. QUICK WINS (exact file:line, implement now)

1. **approvals/page.tsx:96-108 — fix failing contrast on metadata labels.** Replace `text-zinc-500` (dl, line 96) and the three `text-zinc-400` dt classes (lines 98, 103, 107) with `text-muted`; replace line 95 `text-zinc-600 dark:text-zinc-400` with `text-muted`. zinc-400 on white ≈ 2.5:1 — fails WCAG AA.
2. **approvals/page.tsx:99+103 — link and truncate the ids.** Wrap run id in `<Link href={`/runs?run_id=${a.run_id}`}>` with `hover:text-accent underline-offset-2`, and render via `Mono` truncated to ~8 chars with `title={full}` (`{a.run_id.slice(0, 8)}…`).
3. **ui.tsx:247 — cap Json height.** Add `max-h-80` to the `pre` classes so giant output snapshots don't blow up page height: `"max-h-80 overflow-auto rounded-lg …"`.
4. **audit/page.tsx:49-59 — expansion affordance + timestamp on collapsed rows.** Inside `<summary>` add a chevron that rotates on open: prepend `<svg className="h-3.5 w-3.5 shrink-0 text-muted transition-transform [[open]>summary_&]:rotate-90" …>` (chevron-right path `M9 18l6-6-6-6`), and add `{record.started_at && <span>{new Date(record.started_at).toLocaleTimeString()}</span>}` in the right-hand meta span (before cost). Also add `group-open:` styling or `[&::-webkit-details-marker]:hidden` for safety.
5. **audit/page.tsx:60-76 — render the newly populated typed fields.** After the Output snapshot details, add two more collapsed `<details>` blocks mirroring it: `Tool calls` (`record.tool_calls?.length ? <Json value={record.tool_calls}/> : null`) and, in the dl, a `Tokens` Field when `record.token_usage` exists. The backend fix (memory: finding_audit_field_gap) populated these; the UI still hides them.
6. **runs/page.tsx:83 — consistent row hover.** Replace `hover:opacity-80` with `hover:bg-zinc-50 dark:hover:bg-zinc-800/40 rounded-lg px-2 -mx-2` to match every other hover treatment.
7. **runs/page.tsx:41-45 + 53 — write run selection into the URL.** In the `setSelected` path add `history.replaceState(null, "", runId ? `?run_id=${runId}` : location.pathname)` (small wrapper `select(id)`), so run details are shareable and survive reload.
8. **runs/page.tsx:209-216 — link the paused state to Approvals.** When `data.approval_paused_state` exists, render above the Json: `<Link href="/approvals" className="text-sm text-accent hover:underline">Resolve in Approvals →</Link>` (use `approval_paused_state.approval_id` in copy).
9. **NodeInspector.tsx:87-88 — stop duplicating the field class.** Export `fieldInput` from `ui.tsx` (it's module-local at ui.tsx:212-213) and use it here; the local copy has already drifted (missing `placeholder:text-zinc-500`). Keep the extra `disabled:opacity-60` as an appended class.
10. **NodeInspector.tsx:109+148 — required marker semantics.** Change `text-amber-600` to `text-red-600 dark:text-red-400` for the `*`; amber means pending/warning everywhere else in this app.
11. **studio/edit/page.tsx:281-295 — de-escalate the draft banner.** Keep amber only for the read-only/published branch; for the draft branch switch to a neutral info tone: `border-border bg-surface text-muted` (or indigo `border-accent/30 bg-accent/5 text-foreground`). A permanent amber banner trains users to ignore warnings.
12. **studio/edit/page.tsx:399-431 — move focus into the node dialog.** Add a `ref` to the close Button (or first input) and `useEffect(() => closeRef.current?.focus(), [])`; on close, nothing re-focuses either — return focus to the canvas node isn't trivial, but at minimum initial focus + `aria-modal="true"` on the dialog div.
13. **ui.tsx:161-173 — make unknown *terminal* statuses fail loud.** Add `if (process.env.NODE_ENV !== "production" && !STATUS_TONES[key]) console.warn("StatusBadge: unmapped status", status);` so the policy-kill-rendered-gray class of bug (visible in verify-run-failed.png) can't silently recur.
14. **AppShell.tsx:135-144 — Connect button should reflect connected state.** When `isConfigured()` (needs the `useConnected()` hook), render `variant="default"` with label "Connection" (or a settings glyph) instead of permanent primary "Connect" — primary CTA noise on every page once you're already connected.
15. **cost/page.tsx:36-39 — don't show 4 decimals for dollars ≥ $1.** `total_cost_usd >= 1 ? total.toFixed(2) : total.toFixed(4)` — `$1234.5678` reads wrong; sub-cent precision only matters near zero. (Same rule for audit row cost at audit/page.tsx:55.)
16. **Add a `Skeleton`/loading row to ui.tsx and use it on list pages.** New ~6-line component (`animate-pulse rounded-lg bg-zinc-100 dark:bg-zinc-800 h-10`) rendered when `loading && !data` in runs/page.tsx:75-77, audit/page.tsx:33-35, approvals/page.tsx:37-39, studio/page.tsx:91-92. Currently these pages show *nothing* while loading.
17. **audit/page.tsx:63-64 + approvals SLA (approvals/page.tsx:108) — humanize timestamps.** Add a tiny `fmtTime(iso)` helper (in ui.tsx) → `new Date(iso).toLocaleString()` with `title={iso}` for the raw value; use for Started/Completed/SLA.

## 4b. BIGGER BETS (one-liners with rationale)

- **Overview → live dashboard**: replace the quick-link grid with real data (pending approvals count, last 5 runs w/ status, total spend) — the landing page should answer "does anything need me?" not re-render the nav.
- **Designed run timeline**: replace "Load timeline" + raw JSON with an auto-loaded vertical step timeline (node, status dot, duration, cost) linked to audit records — this is the core observability view and currently it's a JSON dump.
- **Unify run/audit/approval into a run-centric detail view**: tabs or sections on the run page (Timeline · Audit · Approvals · Output) instead of three disconnected top-level pages showing the same execution.
- **Rendered output, not JSON**: detect `formatted`/markdown-ish string outputs and render them (prose), with a "Raw JSON" toggle — succeeded-run output is currently unreadable.
- **Cost page with substance**: per-node/per-run breakdown table + budget bar (the backend econ plane has waste/budget data) — a single number is not a cost view.
- **Real modal/confirm primitives**: replace `window.confirm` (studio delete) and the hand-rolled node dialog with one accessible Dialog component (focus trap, aria-modal, scroll lock).
- **Unsaved-changes guard + autosave in Studio editor**: dirty tracking on nodes/edges/name with beforeunload + in-app nav guard; losing canvas work is a trust-killer.
- **Empty states with onboarding**: "No workflows yet" → illustrated CTA that creates a starter workflow; "No runs yet" → inline example submit; first-run experience currently dead-ends.
- **Manual theme toggle**: dark mode exists only via OS preference; ops users often want to pin one.
- **Backend ask — timestamps on RunStatusResponse**: run list can't show recency (api-types.ts:1457-1487 has no created/submitted time); add `submitted_at` server-side, then sort + display relative times in the run list.

# P0 Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the new dark/teal design system, app shell, and a live Overview screen — the reviewable slice that proves the handoff design end-to-end against the real backend.

**Architecture:** Keep the typed API client (`app/lib/api.ts` + regenerated `api-types.ts`) and `config.ts`; replace all styles/shell/pages. New `globals.css` design tokens (Tailwind 4 `@theme` + CSS vars), a client `AppShell` (Sidebar + Topbar + Toast + Connect bar) rendered by the server `layout.tsx`, and an Overview page wired to `/health`, `/v1/metrics`, `/v1/deployments`, `/v1/admin/runs`.

**Tech Stack:** Next.js **16** (static export, App Router) · React 19 · Tailwind 4 · @fontsource IBM Plex Sans/Mono · existing `apiFetch` client · vitest.

**Code-detail policy (ponytail):** load-bearing/tricky units (tokens, shell, hooks, contexts, api additions) are given as complete literal code. Repetitive compositional JSX (tile grids, list rows) is specified by exact structure + token classes + the exact API call — the executor writes the markup directly; a literal 400-line JSX dump in the plan would be longer than the code it describes. Non-trivial pure logic gets a vitest test; static presentational components get a live-render verification step instead.

**Pre-req (every task):** Next.js 16 has breaking changes — before writing any Next-specific code, read the relevant guide under `frontend/node_modules/next/dist/docs/` (routing, layouts, `"use client"`, `output: export`, metadata). See `frontend/AGENTS.md`.

**Versioning (P0):** repo policy — every code-affecting commit bumps `pyproject.toml`.
Intermediate P0 commits bump the **Fix** segment, continuing the chain:
`0.10.0.0.4, 0.10.0.0.5, …` (stage `pyproject.toml` with each commit below).
The **final** phase commit (Task 9 Step 5) instead does the **Med** bump that caps
the phase: `→ 0.10.1`. (User chose Med-per-phase; P1 caps at `0.10.2`, etc.)

---

### Task 0: Regenerate API surface + add fonts, establish baseline

**Files:**
- Modify: `frontend/openapi.json` (regenerate)
- Modify: `frontend/app/lib/api-types.ts` (regenerate)
- Modify: `frontend/package.json` (add @fontsource deps)

- [ ] **Step 1: Regenerate the OpenAPI spec + types from the current backend**

Run (from repo root — the stale spec is missing `retention` and other routes):
```bash
cd frontend && npm run gen:api
```
`gen:api` = `uv run --project .. python ../scripts/dump_openapi.py --out openapi.json && openapi-typescript openapi.json -o app/lib/api-types.ts`.
If `gen:api` fails with `No module named 'zeroth'`, run the dump with `PYTHONPATH`:
```bash
cd /Users/macae/Documents/projects/zeroth-core-main && PYTHONPATH=src uv run python scripts/dump_openapi.py --out frontend/openapi.json && cd frontend && npx openapi-typescript openapi.json -o app/lib/api-types.ts
```
Expected: `openapi.json` now has **62 paths** incl. `/v1/retention/*`; `api-types.ts` regenerates cleanly.

- [ ] **Step 2: Verify the retention routes are present (drift check)**

Run: `grep -c "/v1/retention/" frontend/openapi.json`
Expected: `>= 4` (was `0` before).

- [ ] **Step 3: Add self-hosted IBM Plex fonts (offline-safe for static export)**

Run:
```bash
cd frontend && npm i @fontsource/ibm-plex-sans @fontsource/ibm-plex-mono
```
Rationale: handoff uses IBM Plex; `next/font/google` is avoided here (see `layout.tsx` comment — offline/CI static export). @fontsource vendors woff2 locally.

- [ ] **Step 4: Baseline build still green before touching UI**

Run: `cd frontend && npx tsc --noEmit && npm test`
Expected: typecheck passes; existing `runEligibility.test.ts` passes.

- [ ] **Step 5: Commit**
```bash
git add frontend/openapi.json frontend/app/lib/api-types.ts frontend/package.json frontend/package-lock.json
git commit -m "chore(console): regenerate OpenAPI+types, add IBM Plex fonts"
```

---

### Task 1: Design tokens — replace globals.css

**Files:**
- Modify: `frontend/app/globals.css` (full replacement)

- [ ] **Step 1: Replace `globals.css` with the handoff token system (dark-only)**

```css
@import "tailwindcss";

/* Self-hosted IBM Plex (offline-safe). Weights used by the design. */
@import "@fontsource/ibm-plex-sans/400.css";
@import "@fontsource/ibm-plex-sans/500.css";
@import "@fontsource/ibm-plex-sans/600.css";
@import "@fontsource/ibm-plex-sans/700.css";
@import "@fontsource/ibm-plex-mono/400.css";
@import "@fontsource/ibm-plex-mono/500.css";
@import "@fontsource/ibm-plex-mono/600.css";

:root {
  color-scheme: dark;
  /* backgrounds */
  --bg-page:#0b0d11; --bg-chrome:#0d1015; --bg-card:#11141a;
  --bg-raised:#141822; --bg-raised-2:#171b23; --bg-code:#0d1015;
  /* text */
  --text-primary:#e7eaf0; --text-secondary:#c6ccd8; --text-muted:#8f97a6;
  --text-faint:#5c6472; --text-disabled:#3a4150; --text-code:#a9b2c2;
  /* accent + semantic */
  --accent:#5eead4; --success:#86efac; --warning:#fcd34d; --danger:#f87171;
  --info:#93c5fd; --agent:#c4b5fd; --neutral:#a3adc2;
  /* node-type colors */
  --nt-entrypoint:#5eead4; --nt-agent:#c4b5fd; --nt-exec:#93c5fd;
  --nt-approval:#fcd34d; --nt-retrieval:#86efac; --nt-subgraph:#a3adc2;
  /* borders */
  --hair:rgba(255,255,255,0.07); --hair-strong:rgba(255,255,255,0.1);
}

@theme inline {
  --color-page:var(--bg-page); --color-chrome:var(--bg-chrome);
  --color-card:var(--bg-card); --color-raised:var(--bg-raised);
  --color-raised2:var(--bg-raised-2); --color-code:var(--bg-code);
  --color-primary:var(--text-primary); --color-secondary:var(--text-secondary);
  --color-muted:var(--text-muted); --color-faint:var(--text-faint);
  --color-accent:var(--accent); --color-success:var(--success);
  --color-warning:var(--warning); --color-danger:var(--danger);
  --color-info:var(--info); --color-agent:var(--agent); --color-neutral:var(--neutral);
  --color-hair:var(--hair);
  --font-sans:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --font-mono:"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}

html, body { height:100%; }
body {
  background:var(--bg-page); color:var(--text-primary);
  font-family:var(--font-sans); font-size:13.5px;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  overflow:hidden; /* the app shell owns scrolling */
}

::selection { background:rgba(94,234,212,0.25); }
*:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:6px; }

/* scrollbars: #242a35 thumb on transparent */
* { scrollbar-color:#242a35 transparent; scrollbar-width:thin; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:#242a35; border-radius:6px; }
::-webkit-scrollbar-track { background:transparent; }

@keyframes zpulse { 0%,100%{opacity:1} 50%{opacity:.35} }
@keyframes zfade { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
.z-pulse { animation:zpulse 1.8s ease-in-out infinite; }
.z-fade { animation:zfade .25s ease-out; }

/* React Flow retuned to the dark tokens (canvas skinned in P2). */
.react-flow {
  --xy-background-color:var(--bg-page);
  --xy-node-border-default:var(--hair-strong);
  --xy-controls-button-background-color:var(--bg-card);
  --xy-controls-button-background-color-hover:var(--bg-raised);
  --xy-controls-button-color:var(--text-primary);
  --xy-controls-button-border-color:var(--hair);
  --xy-minimap-background-color:var(--bg-card);
  --xy-edge-stroke:var(--text-faint);
}
.react-flow__controls-button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
```

- [ ] **Step 2: Verify tokens compile (Tailwind picks up `@theme`)**

Run: `cd frontend && npx tailwindcss --input app/globals.css --output /tmp/t.css 2>&1 | tail -3 || true`
Then confirm no CSS syntax errors during `npm run build` later (Task 9). Quick check now: `npx tsc --noEmit` unaffected.

- [ ] **Step 3: Commit**
```bash
git add frontend/app/globals.css
git commit -m "feat(console): dark/teal design tokens per handoff"
```

---

### Task 2: Logo assets

**Files:**
- Create: `frontend/public/zeroth-mark.svg`, `frontend/public/zeroth-logo.svg`

- [ ] **Step 1: Copy the real logo SVGs into `public/`**
```bash
cp docs/assets/logo/zeroth-mark.svg frontend/public/zeroth-mark.svg
cp docs/assets/logo/zeroth-logo-dark.svg frontend/public/zeroth-logo.svg
```
(Handoff logo is typographic; §Assets says substitute the repo's real SVGs.)

- [ ] **Step 2: Commit**
```bash
git add frontend/public/zeroth-mark.svg frontend/public/zeroth-logo.svg
git commit -m "chore(console): vendor real logo SVGs into public/"
```

---

### Task 3: UI primitives

**Files:**
- Create: `frontend/app/components/primitives/Card.tsx`, `Pill.tsx`, `StatusDot.tsx`, `MonoLabel.tsx`, `Button.tsx`, `CodeBlock.tsx`, `Skeleton.tsx`, `index.ts`

Primitives encode the handoff's repeated atoms so screens stay DRY. Each is a thin
styled wrapper — no logic. Exact contracts:

- [ ] **Step 1: `StatusDot.tsx`** — 8px round dot; prop `tone: "accent"|"success"|"warning"|"danger"|"muted"|"info"|"agent"|"neutral"`, `pulse?: boolean`. Maps tone→`var(--…)` background; `pulse` adds class `z-pulse`. Also export a `nodeTypeColor(type)` helper returning the `--nt-*` var for entrypoint/agent/exec/approval/retrieval/subgraph.

```tsx
const TONE: Record<string,string> = {
  accent:"var(--accent)", success:"var(--success)", warning:"var(--warning)",
  danger:"var(--danger)", muted:"var(--text-faint)", info:"var(--info)",
  agent:"var(--agent)", neutral:"var(--neutral)",
};
export function StatusDot({ tone="muted", pulse=false }:{tone?:keyof typeof TONE|string; pulse?:boolean}) {
  return <span className={pulse?"z-pulse":""} style={{
    display:"inline-block", width:8, height:8, borderRadius:"50%",
    background:TONE[tone as string]??tone }} aria-hidden />;
}
export const NODE_TYPE_COLOR: Record<string,string> = {
  entrypoint:"var(--nt-entrypoint)", agent:"var(--nt-agent)", exec:"var(--nt-exec)",
  approval:"var(--nt-approval)", retrieval:"var(--nt-retrieval)", subgraph:"var(--nt-subgraph)",
};
```

- [ ] **Step 2: `Card.tsx`** — `<section>` bg `var(--bg-card)`, 1px `var(--hair)`, radius 8, padding 14–16 (prop `pad?`). Optional `label` (mono uppercase 10.5px `var(--text-muted)`, ls .08em) header slot.

- [ ] **Step 3: `Pill.tsx`** — small bordered pill; prop `tone` (same map) → text color + `color-mix(in srgb, <tone> 12%, transparent)` bg + `color-mix(... 30% ...)` border; mono uppercase 10.5px. Used for run status / lifecycle / env badges.

- [ ] **Step 4: `Button.tsx`** — variants `primary` (teal tint bg `rgba(94,234,212,.12)`, teal text, hover brightens), `neutral` (hairline border), `danger` (red tint). Radius 6, mono-ish 12px, disabled dims. `<button>` with `aria` pass-through.

- [ ] **Step 5: `MonoLabel.tsx`** (mono uppercase section label), `CodeBlock.tsx` (`<pre>` bg `var(--bg-code)`, mono 12.5px, line-height 1.75, `overflow-x:auto`, copy button), `Skeleton.tsx` (pulsing `var(--bg-raised)` block for loading states).

- [ ] **Step 6: `index.ts`** re-exports all primitives.

- [ ] **Step 7: Verify typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS (no consumers yet; just compile).

- [ ] **Step 8: Commit**
```bash
git add frontend/app/components/primitives
git commit -m "feat(console): UI primitives (Card, Pill, StatusDot, Button, CodeBlock)"
```

---

### Task 4: Toast context

**Files:**
- Create: `frontend/app/components/Toast.tsx`
- Test: `frontend/app/components/toast.test.tsx` (optional — logic is trivial; skip if no reducer)

- [ ] **Step 1: Toast provider + hook**

```tsx
"use client";
import { createContext, useContext, useState, useCallback, useRef } from "react";
type Toast = { id:number; msg:string };
const Ctx = createContext<(msg:string)=>void>(()=>{});
export const useToast = () => useContext(Ctx);
export function ToastProvider({ children }:{children:React.ReactNode}) {
  const [items,setItems] = useState<Toast[]>([]);
  const seq = useRef(0);
  const push = useCallback((msg:string)=>{
    const id = ++seq.current;
    setItems(x=>[...x,{id,msg}]);
    setTimeout(()=>setItems(x=>x.filter(t=>t.id!==id)), 3200);
  },[]);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div style={{position:"fixed",right:16,bottom:16,display:"flex",flexDirection:"column",gap:8,zIndex:60}}>
        {items.map(t=>(
          <div key={t.id} className="z-fade" style={{
            background:"var(--bg-raised-2)", border:"1px solid rgba(94,234,212,0.35)",
            borderRadius:8, padding:"9px 12px", fontFamily:"var(--font-mono)", fontSize:12,
            color:"var(--text-primary)", maxWidth:360 }}>{t.msg}</div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
```
(No timer-based unit test — `setTimeout` cleanup is trivial; verified live in Task 9.)

- [ ] **Step 2: Verify typecheck**  · Run: `npx tsc --noEmit` → PASS

- [ ] **Step 3: Commit** · `git commit -m "feat(console): toast provider"`

---

### Task 5: `usePolling` hook (TDD)

**Files:**
- Create: `frontend/app/hooks/usePolling.ts`
- Test: `frontend/app/hooks/usePolling.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { computeNextDelay } from "./usePolling";

describe("computeNextDelay", () => {
  it("returns base interval when active and visible", () => {
    expect(computeNextDelay({ active:true, hidden:false, base:2000 })).toBe(2000);
  });
  it("returns null (pause) when inactive", () => {
    expect(computeNextDelay({ active:false, hidden:false, base:2000 })).toBeNull();
  });
  it("backs off to 4x when tab hidden", () => {
    expect(computeNextDelay({ active:true, hidden:true, base:2000 })).toBe(8000);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run app/hooks/usePolling.test.ts`
Expected: FAIL — `computeNextDelay` not exported.

- [ ] **Step 3: Implement**

```ts
import { useEffect, useRef } from "react";

export function computeNextDelay(
  { active, hidden, base }:{ active:boolean; hidden:boolean; base:number }
): number | null {
  if (!active) return null;
  return hidden ? base * 4 : base;
}

/** Calls `fn` every `base` ms while `active`; pauses when inactive, backs off 4x
 *  when the tab is hidden. Fires once immediately on activation. */
export function usePolling(fn: () => void, base: number, active: boolean) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    saved.current();
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const delay = computeNextDelay({ active, hidden: document.hidden, base });
      if (delay == null) return;
      timer = setTimeout(() => { saved.current(); tick(); }, delay);
    };
    tick();
    return () => clearTimeout(timer);
  }, [active, base]);
}
```

- [ ] **Step 4: Run test to verify it passes** · Run: `npx vitest run app/hooks/usePolling.test.ts` → PASS

- [ ] **Step 5: Commit** · `git commit -m "feat(console): usePolling hook with visibility backoff"`

---

### Task 6: Regulus detection (TDD)

**Files:**
- Create: `frontend/app/lib/regulus.ts`
- Test: `frontend/app/lib/regulus.test.ts`

- [ ] **Step 1: Failing test** — `regulusStatusFrom(response)` maps an HTTP probe result to `"enabled" | "absent"`.

```ts
import { describe, it, expect } from "vitest";
import { regulusStatusFrom } from "./regulus";
describe("regulusStatusFrom", () => {
  it("enabled on 200", () => expect(regulusStatusFrom(200)).toBe("enabled"));
  it("enabled on 401/403 (mounted but auth-gated)", () => {
    expect(regulusStatusFrom(401)).toBe("enabled");
    expect(regulusStatusFrom(403)).toBe("enabled");
  });
  it("absent on 404", () => expect(regulusStatusFrom(404)).toBe("absent"));
});
```

- [ ] **Step 2: Verify fail** · `npx vitest run app/lib/regulus.test.ts` → FAIL

- [ ] **Step 3: Implement**

```ts
import { getApiBase } from "./config";
export type RegulusStatus = "enabled" | "absent" | "unknown";
export function regulusStatusFrom(httpStatus: number): RegulusStatus {
  if (httpStatus === 404) return "absent";     // mount guarded off (no regulus extra)
  return "enabled";                             // 200/401/403 => sub-app is mounted
}
/** Probe the mounted Regulus openapi once. Cheap, unauthenticated GET. */
export async function detectRegulus(): Promise<RegulusStatus> {
  try {
    const res = await fetch(`${getApiBase()}/regulus/openapi.json`, { method:"GET" });
    return regulusStatusFrom(res.status);
  } catch { return "unknown"; }
}
```

- [ ] **Step 4: Verify pass** · `npx vitest run app/lib/regulus.test.ts` → PASS

- [ ] **Step 5: Commit** · `git commit -m "feat(console): regulus mount detection"`

---

### Task 7: Connect bar

**Files:**
- Create: `frontend/app/components/ConnectBar.tsx`
- Modify: `frontend/app/lib/config.ts` (add env + tenant helpers)

- [ ] **Step 1: Extend `config.ts`** — add optional env label + tenant persisted the same way:

```ts
const ENV_KEY = "zeroth.env";      // "local" | "staging" | "production"
const TENANT_KEY = "zeroth.tenant";
export function getEnv(): string { return read(ENV_KEY) ?? "local"; }
export function getTenant(): string { return read(TENANT_KEY) ?? "default"; }
export function setEnvTenant(env:string, tenant:string){ write(ENV_KEY, env); write(TENANT_KEY, tenant); }
```
(where `read`/`write` are the existing localStorage guards — refactor the current inline getters to share them; keep `getApiBase`/`getApiKey`/`setConfig`/`isConfigured` behavior unchanged.)

- [ ] **Step 2: `ConnectBar.tsx`** — modal opened from the sidebar footer. Inputs: API base URL (placeholder `same-origin`), API key (`type=password`, masked), env `<select>` (local/staging/production), tenant. Save → `setConfig` + `setEnvTenant` → `onSaved()` (triggers a data refetch via a bumped React key or `location.reload()` — lazy: reload). **Security:** the key stays in localStorage only; never logged, never in a URL/query.

- [ ] **Step 3: Verify typecheck** · `npx tsc --noEmit` → PASS

- [ ] **Step 4: Commit** · `git commit -m "feat(console): Connect bar (base/key/env/tenant)"`

---

### Task 8: App shell (Sidebar + Topbar + layout) + route stubs

**Files:**
- Create: `frontend/app/components/nav.ts` (nav model + route→title map)
- Create: `frontend/app/components/Sidebar.tsx`, `Topbar.tsx`, `AppShell.tsx` (replaces old `AppShell.tsx`)
- Modify: `frontend/app/layout.tsx` (full replacement)
- Create stub pages: `frontend/app/{runs,approvals,audit,deployments,studio,templates,connectors,cost,retention,rightsizing,metrics,guide}/page.tsx` and `frontend/app/regulus/{page,capabilities/page,enforcement/page,costing/page,reconciliation/page}.tsx`
- Delete: old `frontend/app/components/AppShell.tsx` content (superseded), old page bodies get replaced as their phase lands.

- [ ] **Step 1: `nav.ts` — the IA model**

```ts
export type NavItem = { label:string; href:string; badge?:"approvals" };
export type NavGroup = { heading:string; items:NavItem[]; gated?:"regulus" };
export const NAV: NavGroup[] = [
  { heading:"Operate", items:[
    { label:"Overview", href:"/" }, { label:"Runs", href:"/runs" },
    { label:"Approvals", href:"/approvals", badge:"approvals" },
    { label:"Audit", href:"/audit" }, { label:"Deployments", href:"/deployments" } ]},
  { heading:"Build", items:[
    { label:"Studio", href:"/studio" }, { label:"Templates", href:"/templates" },
    { label:"Connectors", href:"/connectors" } ]},
  { heading:"Govern", items:[
    { label:"Cost", href:"/cost" }, { label:"Retention", href:"/retention" },
    { label:"Rightsizing", href:"/rightsizing" }, { label:"Metrics", href:"/metrics" } ]},
  { heading:"Regulus", gated:"regulus", items:[
    { label:"Econ Dashboard", href:"/regulus" }, { label:"Capabilities", href:"/regulus/capabilities" },
    { label:"Enforcement", href:"/regulus/enforcement" }, { label:"Costing", href:"/regulus/costing" },
    { label:"Reconciliation", href:"/regulus/reconciliation" } ]},
  { heading:"Learn", items:[{ label:"Guide", href:"/guide" }]},
];
export const TITLE: Record<string,string> = Object.fromEntries(
  NAV.flatMap(g=>g.items.map(i=>[i.href, i.label]))
);
```

- [ ] **Step 2: `Sidebar.tsx`** (client) — 212px, bg `var(--bg-chrome)`, right hairline. Top: `/public/zeroth-mark.svg` (14px teal square feel) + `zeroth/core` mono 15px (`/core` faint) + `v{VERSION} · console` mono 10.5px (import version from a generated const or hardcode `0.10.0.0.3` — see Step 6). Groups: mono-uppercase heading (10px, ls .12em, `var(--text-faint)`) + items. Item: 5px square dot + 13px label, pad 6/8, radius 6. Active (via `usePathname()` exact match, `/` only active on `/`): teal tint bg `rgba(94,234,212,0.09)` + teal text/dot. Approvals badge: mono 10.5px amber count from a `pendingApprovals` prop (0 in P0; wired P1). **Regulus group** hidden unless `regulusStatus==="enabled"` (from context). Footer: pulsing green `StatusDot` + host (`getApiBase()||"127.0.0.1:8000"`) + masked key `${key.slice(0,13)}••••`; clicking footer opens the Connect bar.

- [ ] **Step 3: `Topbar.tsx`** (client) — 52px, bg `var(--bg-chrome)`, bottom hairline. Left: breadcrumb `{tenant} / {title}` (tenant faint, title white) — title from `TITLE[usePathname()]`. Right: env `Pill` (local muted / staging warning / production danger, from `getEnv()`) + "served: {ref}" chip (mono 11px, bg `var(--bg-card)`) from `listDeployments()` first serving deployment, or `—` while loading.

- [ ] **Step 4: `AppShell.tsx`** (client) — the composition:

```tsx
"use client";
import { useEffect, useState } from "react";
import { ToastProvider } from "./Toast";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { detectRegulus, type RegulusStatus } from "@/app/lib/regulus";
import { RegulusCtx } from "./regulusContext";

export function AppShell({ children }:{children:React.ReactNode}) {
  const [reg,setReg] = useState<RegulusStatus>("unknown");
  useEffect(()=>{ detectRegulus().then(setReg); },[]);
  return (
    <ToastProvider>
      <RegulusCtx.Provider value={reg}>
        <div style={{ display:"flex", height:"100vh", overflow:"hidden" }}>
          <Sidebar />
          <div style={{ display:"flex", flexDirection:"column", flex:1, minWidth:0 }}>
            <Topbar />
            <main id="main" tabIndex={-1} style={{ flex:1, overflowY:"auto" }}
                  className="z-fade">{children}</main>
          </div>
        </div>
      </RegulusCtx.Provider>
    </ToastProvider>
  );
}
```
Also create `frontend/app/components/regulusContext.ts`:
```ts
"use client";
import { createContext, useContext } from "react";
import type { RegulusStatus } from "@/app/lib/regulus";
export const RegulusCtx = createContext<RegulusStatus>("unknown");
export const useRegulus = () => useContext(RegulusCtx);
```

- [ ] **Step 5: Rewrite `layout.tsx`** (server component) — html/body + render `<AppShell>`:

```tsx
import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/app/components/AppShell";
export const metadata: Metadata = {
  title: "Zeroth Console",
  description: "Operate and author Zeroth multi-agent apps",
};
export default function RootLayout({ children }:{ children:React.ReactNode }) {
  return (
    <html lang="en"><body><AppShell>{children}</AppShell></body></html>
  );
}
```
⚠️ Confirm against `node_modules/next/dist/docs/` that a client component tree under a server `layout.tsx` is the correct Next 16 pattern for `output: export`.

- [ ] **Step 6: Version constant** — the sidebar shows the app version. Add `frontend/app/lib/version.ts` `export const VERSION = "0.10.0.0.3";` with a comment to keep it in sync with `pyproject.toml` (ponytail: a build-time inject is overkill for a string that bumps with the same commit).

- [ ] **Step 7: Stub pages** — each non-Overview route gets a placeholder so nav never 404s:
```tsx
export default function Page() {
  return <div style={{maxWidth:1160, margin:"0 auto", padding:"26px 28px"}}>
    <h1 style={{fontSize:20, fontWeight:600}}>Runs</h1>
    <p style={{color:"var(--text-muted)", fontSize:13}}>Implemented in phase P1.</p>
  </div>;
}
```
(Substitute the screen name + phase per route. Overview `/` is Task 9, not a stub.)

- [ ] **Step 8: Verify typecheck + dev boot**

Run: `cd frontend && npx tsc --noEmit`
Expected: PASS. (Live render verified in Task 9.)

- [ ] **Step 9: Commit**
```bash
git add frontend/app/components frontend/app/layout.tsx frontend/app/lib/version.ts frontend/app/*/page.tsx frontend/app/regulus
git commit -m "feat(console): app shell (sidebar, topbar, nav) + route stubs"
```

---

### Task 9: Overview screen (wired to live API)

**Files:**
- Modify: `frontend/app/page.tsx` (full replacement)
- Modify: `frontend/app/lib/api.ts` (add `getMetrics`, `rollbackDeployment` if absent)

- [ ] **Step 1: Add missing api.ts functions (guarded — skip any that already exist)**

```ts
// --- Overview needs ---
export type MetricsResponse = S["MetricsResponse"];  // confirm name in api-types.ts; else use `unknown`
export function getMetrics(): Promise<MetricsResponse> { return apiFetch<MetricsResponse>("/v1/metrics"); }
export function rollbackDeployment(ref: string): Promise<DeploymentSummary> {
  return apiFetch<DeploymentSummary>(`/v1/deployments/${encodeURIComponent(ref)}/rollback`, { method:"POST" });
}
```
If `MetricsResponse` isn't a generated schema name, grep `api-types.ts` for the metrics operation's response type and use that exact name (no `any`).

- [ ] **Step 2: Build `page.tsx`** (client) — the Overview, content max-width 1160, pad 26/28, `zfade`. Sections (exact structure; markup written directly against these specs):
  1. **Header** — H1 20/600 "Overview" + 13px `var(--text-muted)` subtitle.
  2. **Health tiles** — 4-col grid gap 12. Tiles: **API service** (from `getHealth()` — dot success if `status==="ok"`), **Sandbox backend** (docker · hardened — from health components if present, else static label + neutral dot), **Econ plane** (from `useRegulus()`: enabled→"mounted /regulus" success dot, absent→"not enabled" muted dot), **Storage** (postgres + redis — from health components). Each tile = `Card`, mono uppercase label 10.5px, `StatusDot` + 14/600 value, mono 11px sub.
  3. **Deployments card** (left 1.6fr) — rows from `listDeployments()`: mono name + sub, version `Pill` (teal), state `Pill` (serving/registered), **Rollback** `Button` (calls `rollbackDeployment(ref)` → toast → refetch). Footer note (real caveat): "creating/rolling back registers a version; serving requires restart."
  4. **Getting-started checklist** (right 1fr) — 15px checkbox squares; item "Verify the audit chain" reads `localStorage["zeroth.auditVerified"]` (set by the Audit page in P1); others reflect presence of ≥1 deployment / ≥1 run.
  5. **Recent runs** — from `listRuns()`: `StatusDot` (pulse if running) + mono id + graph name; row click → `router.push("/runs")` (detail deep-link lands in P1).
  - Loading → `Skeleton` blocks in each card; error → red-tinted inline card + Retry; empty → "No deployments yet" + link to Studio.

- [ ] **Step 3: Live verification (preview tools)**

1. Start backend: launch the Zeroth service API (`uv run` per repo README) so `/health`, `/v1/deployments`, `/v1/metrics`, `/v1/admin/runs` respond.
2. `preview_start` the frontend dev server (add `.claude/launch.json` entry: `next dev` on port 3000, cwd `frontend`).
3. In the Browser pane, open the console; set base URL + a valid `X-API-Key` via the Connect bar.
4. `read_console_messages` / `preview_logs` → no errors. `read_page` → sidebar groups, Topbar breadcrumb, 4 health tiles, deployments rows render.
5. `computer` screenshot → compare against the handoff Overview (dark bg, teal accents, IBM Plex, tile grid).

Expected: Overview renders pixel-close and populated from the real API; Regulus tile reflects actual mount state.

- [ ] **Step 4: Full build + tests green**

Run: `cd frontend && npm test && npm run build`
Expected: vitest PASS; `next build` (static export) succeeds with no type/lint errors.

- [ ] **Step 5: Bump version + commit**

Med bump that caps P0: set `pyproject.toml` version → `0.10.1` (drops the deeper Fix
segments from the intermediate commits — valid per the integer-chain policy).
```bash
git add frontend/app/page.tsx frontend/app/lib/api.ts pyproject.toml
git commit -m "feat(console): v0.10.1 — Overview screen on new shell, wired to live API"
```

---

## P0 self-review (spec coverage)

- Design tokens (§4) → Task 1 ✓ · Shell (§5) → Task 8 ✓ · IA/nav (§6) → Task 8 `nav.ts` ✓
- Keep API layer / regen types (§3) → Task 0 ✓ · Connect bar (§5) → Task 7 ✓
- Overview (§7.1, all four endpoint groups) → Task 9 ✓ · polling (§3.2) → Task 5 ✓
- Regulus graceful-degrade detection (§3.2) → Task 6 + Task 8 gating ✓
- Loading/empty/error (§8) → Task 9 Step 2 ✓ · a11y basics (§8) → focus ring (Task 1) + `aria` on dots/buttons ✓
- Deferred to later phases (correct): all screens except Overview are stubs (Task 8 Step 7).

**Open confirmations for the executor:** (1) Next 16 client-tree-under-server-layout pattern for `output: export` — verify in bundled docs. (2) exact generated type name for `/v1/metrics` response. (3) `rollbackDeployment` may already exist in `api.ts` — grep before adding.

# P3 Govern — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Build the four Govern screens — Cost, Retention, Rightsizing & Efficiency, Metrics — on the P0 shell, wired to the live API, replacing the legacy Cost page and the P0 stubs.

**Architecture:** Fresh screens on the P0 primitives (like P1/Templates/Connectors). Rightsizing absorbs the existing `ModelRightsizing.tsx` behavior (re-skinned or rebuilt). Extend `api.ts` with tenant-cost/budget, retention, and quality-verdict wrappers (rightsizing/waste/unit-economics/metrics/manifests already exist).

**Tech Stack:** Next 16 (static export), React 19, Tailwind 4, existing `apiFetch` client, vitest.

**Backend for verification:** service on `:8000` (key `demo-operator-secret-p1`), dev console on `:3000`. Cost/rightsizing have thin data until runs accrue; verify the empty/loaded/error states + the budget-cap and erasure mutations.

**Versioning:** intermediate commits bump Fix (`0.10.3.0.1, …`); phase-cap → Med `0.10.4`.

**Global rules:** dark-only; mutation → optimistic UI + toast; loading/empty/error each screen; API key only in localStorage. Read `frontend/node_modules/next/dist/docs/` before Next-specific code.

---

### Task 0: api wrappers — tenant cost/budget, retention, quality-verdict

**Files:** Modify `app/lib/api.ts`. Confirmed schema names (use exactly; read the field shapes in `api-types.ts`):
```ts
// Cost / budget
export type TenantCost = S["TenantCostResponse"];
export type TenantBudgetRequest = S["TenantBudgetRequest"];
export function getTenantCost(tenantId: string): Promise<TenantCost> {
  return apiFetch(`/v1/tenants/${encodeURIComponent(tenantId)}/cost`);
}
export function setTenantBudget(tenantId: string, body: TenantBudgetRequest): Promise<TenantCost> {
  return apiFetch(`/v1/tenants/${encodeURIComponent(tenantId)}/budget`, { method: "PUT", body: JSON.stringify(body) });
}
// Retention
export type RetentionPolicy = S["RetentionPolicyResponse"];
export type RetentionPolicyBody = S["RetentionPolicyBody"];
export type LegalHold = S["LegalHoldResponse"];
export type LegalHoldBody = S["LegalHoldBody"];
export type ErasureResult = S["ErasureResponse"];
export type ErasureRequestBody = S["ErasureRequestBody"];
export function getRetentionPolicy(): Promise<RetentionPolicy> { return apiFetch("/v1/retention/policy"); }
export function putRetentionPolicy(body: RetentionPolicyBody): Promise<RetentionPolicy> {
  return apiFetch("/v1/retention/policy", { method: "PUT", body: JSON.stringify(body) });
}
export function placeLegalHold(body: LegalHoldBody): Promise<LegalHold> {
  return apiFetch("/v1/retention/legal-holds", { method: "POST", body: JSON.stringify(body) });
}
export function releaseLegalHold(holdId: string): Promise<LegalHold> {
  return apiFetch(`/v1/retention/legal-holds/${encodeURIComponent(holdId)}`, { method: "DELETE" });
}
export function requestErasure(body: ErasureRequestBody): Promise<ErasureResult> {
  return apiFetch("/v1/retention/erasure-requests", { method: "POST", body: JSON.stringify(body) });
}
// Econ quality verdict
export type QualityVerdictRequest = S["QualityVerdictRequest"];
export type RunQualityVerdict = S["RunQualityVerdict"];
export function attachQualityVerdict(body: QualityVerdictRequest): Promise<RunQualityVerdict> {
  return apiFetch("/v1/econ/quality-verdict", { method: "POST", body: JSON.stringify(body) });
}
```
Read `api-types.ts` to confirm each request body's required fields before building the forms. Verify `npx tsc --noEmit` → 0. Don't commit.

---

### Task 1: Cost — `app/cost/page.tsx` (replace legacy)

Handoff README §6. Build fresh on P0 primitives (drop `ui.tsx`):
- [ ] 3 stat cards (`repeat(3, minmax(0,1fr))` — the `minmax(0,…)` matters; give long env-var notes `overflow-wrap:anywhere`):
  1. **Month-to-date spend**: mono 26px amount (from `getTenantCost(getTenant())`), 6px progress bar (teal fill on `#1a1f29` track, animated width), "% of cap · tenant".
  2. **Budget cap (USD)**: mono input + **Set** Button → `setTenantBudget(tenant, {...})` → toast "enforced pre-LLM via /regulus"; note about fail-open default / `ZEROTH_REGULUS__FAIL_CLOSED=true`.
  3. **Per-run ceiling**: value + note about `ZEROTH_REGULUS__PER_RUN_CAP_USD`.
- [ ] Two cards below: **Spend by deployment** (label + amount + 4px colored bar = share of MTD; from `listDeployments()` + `getCostOf(ref)`), **Top nodes by attributed cost** (node-type square + node + detail + amount; from run evidence / unit-economics if available).
- [ ] States + verify tsc/build. Don't commit.

---

### Task 2: Retention & Compliance — `app/retention/page.tsx` (replace stub)

Handoff README §9:
- [ ] Left card: retention TTL rows (scope → mono teal TTL) from `getRetentionPolicy()`; make editable → `putRetentionPolicy(body)` on save. **Legal holds** amber card (id, scope, "TTLs suspended") — place (`placeLegalHold`) / release (`releaseLegalHold`).
- [ ] Right card: erasure requests — subject/fields/note; a form → `requestErasure(body)`; pending shows **Execute erasure** (teal) which flips status to `ERASED` (green) with a chain-integrity toast. Footer explains per-field commitments.
- [ ] States + verify tsc/build. Don't commit.

---

### Task 3: Rightsizing & Efficiency — `app/rightsizing/page.tsx` (replace stub)

New screen (handoff extends the design language). Absorb the existing `ModelRightsizing.tsx` behavior:
- [ ] **Opportunities** from `getRightsizingOpportunities()` — table of nodes by spend + projected savings + experiment-ready flag.
- [ ] **Suggest** (`getRightsizing({...})`) — incumbent vs candidate models (blended $/Mtok, savings %). **Run experiment** (`runRightsizingExperiment({...})`) — measured equivalence report.
- [ ] **Unit economics** (`getUnitEconomics()`) — success rate, cost per successful run, failure tax; by-workflow / by-tenant. **Waste** (`getWaste()`) — findings rollup.
- [ ] Re-skin to P0 primitives; keep the `ModelRightsizing` calc logic if reused. States + verify tsc/build. Don't commit.

---

### Task 4: Metrics — `app/metrics/page.tsx` (replace stub)

- [ ] `getMetrics()` (open `unknown` body — render as a formatted `CodeBlock` + surface any recognizable numeric fields as stat tiles if present). **Manifests** from `listManifests()` — table (name/version/status). Small observability page; keep it lean (ponytail — don't fabricate charts for an open payload).
- [ ] States + verify tsc/build. Don't commit.

---

### Task 5: Verify + phase cap

- [ ] Live verify (backend + preview): Cost (set a budget cap → toast, MTD bar), Retention (edit a TTL, place/release a hold, request+execute an erasure), Rightsizing (opportunities/unit-economics load or empty), Metrics (payload renders). Screenshot each vs. handoff. `read_console_messages` clean.
- [ ] `npm test && npm run build` green. Med cap → `pyproject.toml` `0.10.4`, `version.ts` `0.10.4`, `uv lock` to sync.
```bash
git commit -m "feat(console): v0.10.4 — Govern screens (Cost, Retention, Rightsizing, Metrics)"
```

---

## Self-review checklist
- DESIGN §11 Cost (tenant cost/budget + deployment cost), Retention (5 ops), Rightsizing/econ (6 ops), Metrics/manifests mapped ✓
- No `any` in api additions · loading/empty/error each screen ✓ · legacy cost page + ModelRightsizing retired/absorbed ✓

# P1 Operate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Build the four Operate screens — Runs, Approvals, Audit, Deployments — on the P0 shell, wired to the live API, replacing the legacy pages.

**Architecture:** Reuse the P0 design system (tokens + `app/components/primitives/`), `useToast`, `usePolling`, `useLoad` (lift the P0 Overview `useLoad` into `app/hooks/useLoad.ts` so all screens share it — small refactor). Master-detail + timeline patterns per the handoff README §§3–5. Extend `app/lib/api.ts` with the missing endpoint wrappers (typed via generated `api-types.ts`).

**Tech Stack:** Next 16 (static export), React 19, Tailwind 4, existing `apiFetch` client, vitest.

**Backend for verification:** the service is bootable with data — `export ZEROTH_SERVICE_API_KEYS_JSON='[{"credential_id":"demo","secret":"demo-operator-secret-p1","subject":"demo-operator","roles":["operator","reviewer","admin"]}]'` then `uv run zeroth-core seed-demo` and `ZEROTH_CONSOLE_CORS_ORIGINS=http://localhost:3000 uv run zeroth-core serve --port 8000`. Dev console connects via Connect bar (base `http://localhost:8000`, that key). To exercise runs/approvals, submit a run (needs an LLM key, e.g. `OPENAI_API_KEY`) or seed richer fixtures.

**Versioning:** intermediate commits bump Fix (`0.10.1.0.2, …`); the phase-cap commit bumps Med → `0.10.2`.

**Global rules:** dark-only; every mutation → optimistic UI + toast; every screen handles loading/empty/error; API key only in localStorage, never logged/in a URL. Read `frontend/node_modules/next/dist/docs/` before Next-specific code.

---

### Task 0: Shared plumbing — lift `useLoad`, add missing api.ts wrappers

**Files:** Create `app/hooks/useLoad.ts`; Modify `app/page.tsx` (import from the hook), `app/lib/api.ts`.

- [ ] **Step 1:** Extract the `useLoad<T>` hook + `Loadable<T>` type from `app/page.tsx` into `app/hooks/useLoad.ts` (verbatim — it already handles cancel/reload/keep-last-data). Update `app/page.tsx` to import it. Run `npx tsc --noEmit` + `npm run build` → both green (no behavior change).

- [ ] **Step 2:** Add the missing typed wrappers to `api.ts` (GREP first; add only if absent). Use the exact generated schema types — open `api-types.ts` to confirm each response/request type name; do not use `any`.

```ts
// --- Runs (admin actions + evidence + chain) ---
export type RunEvidence = S["RunEvidenceResponse"];        // confirm exact name in api-types.ts
export function cancelRun(runId: string): Promise<RunStatus> {
  return apiFetch(`/v1/admin/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
}
export function interruptRun(runId: string): Promise<RunStatus> {
  return apiFetch(`/v1/admin/runs/${encodeURIComponent(runId)}/interrupt`, { method: "POST" });
}
export function replayRun(runId: string): Promise<RunInvocationResponse> {
  return apiFetch(`/v1/admin/runs/${encodeURIComponent(runId)}/replay`, { method: "POST" });
}
export function getRunEvidence(runId: string): Promise<RunEvidence> {
  return apiFetch(`/v1/runs/${encodeURIComponent(runId)}/evidence`);
}
export function verifyRunChain(runId: string): Promise<AuditVerification> {
  return apiFetch(`/v1/runs/${encodeURIComponent(runId)}/verify-chain`, { method: "POST" });
}
// --- Deployment detail (ref-parameterized; existing attestation fns use the default ref) ---
export function getDeploymentTimeline(ref: string): Promise<S["DeploymentTimelineResponse"]> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/timeline`);
}
export function getDeploymentEvidence(ref: string): Promise<S["DeploymentEvidenceResponse"]> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/evidence`);
}
export function getInputContract(ref: string): Promise<unknown> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/input-contract`);
}
export function getOutputContract(ref: string): Promise<unknown> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/output-contract`);
}
export function getResultErrorStateSchema(ref: string): Promise<unknown> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/result-error-state-schema`);
}
export function getDeploymentMetadata(ref: string): Promise<S["DeploymentMetadataResponse"]> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/metadata`);
}
export function verifyDeploymentAuditChain(ref: string): Promise<AuditVerification> {
  return apiFetch(`/v1/deployments/${encodeURIComponent(ref)}/audit-verification`);
}
```
Confirm whether `replayRun`, and ref-parameterized attestation/cost/audits variants already exist; reuse if so. The existing `getDeploymentAttestation`/`verifyDeploymentAttestation`/`getCost`/`listAudits`/`listNodeAudits` take no ref (they call `deploymentRef()`); add `ref`-taking variants where a screen must target a selected (non-default) deployment. Contract endpoints return open JSON — render via `CodeBlock` (raw JSON) first; bespoke layouts are a later refinement (ponytail).

Verify: `npx tsc --noEmit` → 0. Do NOT commit (controller commits the batch).

---

### Task 1: Runs (master-detail) — `app/runs/page.tsx`

Replaces the legacy Runs page. Handoff README §3.

- [ ] **Step 1:** Left list (330px): filter chips (all / running / succeeded / failed / awaiting approval — mono 11px, active teal tint); rows from `listRuns()` — StatusDot (pulse when running) + mono id + uppercase status (tone via a `RUN_TONE` map — reuse the one from Overview; lift it to `app/components/runTone.ts`), second line `graph@version · HH:MM:SS · $cost`.
- [ ] **Step 2:** Detail: selected run from `getRun(id)`; mono id 17/600 + status Pill; **Cancel** (danger, only while running/awaiting) → `cancelRun`, **Interrupt** (only while running) → `interruptRun`, **Replay** → `replayRun` (jumps to the new run). Meta row: graph, thread, cost, started.
- [ ] **Step 3:** Failure banner for failed runs (red-tinted `CodeBlock`, the contract-violation error string). Node timeline card from `getRunTimeline(id)` — one row per node: StatusDot, node-type square (`NODE_TYPE_COLOR`), mono node name (150px), uppercase type (90px), note (flex), duration + cost right-aligned. Running rows show `…` + pulse; queued rows dim.
- [ ] **Step 4:** **Evidence** panel from `getRunEvidence(id)` (raw JSON in `CodeBlock`), **Verify chain** button → `verifyRunChain(id)` / `getRunAuditVerification(id)` with the idle→verifying→intact chip; on success set `localStorage["zeroth.auditVerified"]="1"` (completes the Overview checklist). **Invoke** `CodeBlock` with ready-made cURL (`POST /v1/runs`, `X-API-Key`, `thread_id`).
- [ ] **Step 5:** Live polling: while the selected run is `running`/`queued`/`paused_for_approval`, `usePolling` refetches `getRun` + `getRunTimeline` (~2s) so nodes advance live; also refresh the list. Deep-link: reading `?run=<id>` selects it (the Overview recent-runs row and canvas Run jump here).
- [ ] **Step 6:** States: loading skeletons, empty ("No runs yet"), error inline+retry. Verify tsc + build green.

---

### Task 2: Approvals — `app/approvals/page.tsx`

Replaces legacy. Handoff README §4.

- [ ] **Step 1:** Cards from `listApprovals()` — one per approval: header amber square + mono node name + "in {graph} · {run}" + status Pill (pending amber / approved success / rejected danger); pending cards get an amber-tinted border.
- [ ] **Step 2:** Body grid `1fr 220px`: left = "Payload under review" JSON in `CodeBlock` (PII shown as returned/redacted); right = **Approve** (success) / **Reject** (danger) → `resolveApproval(id, {decision, note})` + note "Requires reviewer role. Recorded to the audit chain." Decided cards show the decision record (who/when/note) instead of buttons.
- [ ] **Step 3:** On resolve: optimistic status flip + toast; refetch; the sidebar Approvals badge count = number of pending approvals (thread this count from a shared source — see Task 5).
- [ ] **Step 4:** States (loading/empty "No approvals pending"/error). Verify tsc + build.

---

### Task 3: Audit — `app/audit/page.tsx`

Replaces legacy. Handoff README §5.

- [ ] **Step 1:** Header: title + right-aligned chain-status chip + **Verify chain** primary button. Chip states idle ("last verified …") → verifying (teal, count) → intact (success). On success set `localStorage["zeroth.auditVerified"]="1"`.
- [ ] **Step 2:** Table (all mono) from `listAudits()` / `listNodeAudits()`: seq 48 · time 66 · run 110 · node 110 · event flex · digest 170 (`sha256:…`, faint) · sig 34. Event color by kind: ok secondary, warn warning (e.g. `secret.redacted`, `approval.requested · held`), denied danger with faint red row bg (e.g. `policy.denied`, `contract.violation`). After verify, the `sig ✓` column turns success-green.
- [ ] **Step 3:** Verify via `getDeploymentAuditChain`/`verifyDeploymentAuditChain(ref)`. Footer note on chain-safe crypto-erasure. States + verify tsc/build.

---

### Task 4: Deployments (master-detail, NEW) — `app/deployments/page.tsx`

Replaces the P0 stub. Handoff README §1 (deployments card) expanded into a full screen.

- [ ] **Step 1:** Left list from `listDeployments()` — mono ref + `graph@version`, serving/registered Pill, selected row teal tint.
- [ ] **Step 2:** Detail header: ref + version Pill + serving Pill; **Rollback** (with proper target-version selection from the timeline — fixes the P0 Overview limitation), **Create deployment** (from a published workflow via `createDeployment`).
- [ ] **Step 3:** Tabs / stacked cards: **Metadata** (`getDeploymentMetadata`), **Contracts** (input/output-contract + result-error-state-schema → `CodeBlock`), **Attestation** (`getDeploymentAttestation(ref)` + **Verify** GET `attestation/verify` and POST `verify-attestation`), **Evidence** (`getDeploymentEvidence`), **Timeline** (`getDeploymentTimeline` — version history; this is where Rollback picks its target), **Cost** (`getCost`/ref cost), **Audits** (`listAudits`/ref → link to Audit screen).
- [ ] **Step 4:** States + verify tsc/build. Each mutation toasts.

---

### Task 5: Sidebar approvals badge wiring

**Files:** Modify `app/components/AppShell.tsx`, `app/components/Sidebar.tsx`.

- [ ] **Step 1:** In `AppShell`, fetch pending-approvals count once on mount (+ refresh on a slow `usePolling`, ~15s) via `listApprovals()` (count `status==="pending"`), pass to `<Sidebar pendingApprovals={n}>`. Guard errors → 0. This lights the amber badge (already rendered when >0). Verify tsc/build.

---

### Task 6: Live verification + phase cap

- [ ] **Step 1:** Boot backend (see header) + `preview_start console`; connect via Connect bar. Drive each screen: Runs list/detail/timeline (submit a run to see live polling if an LLM key is available; else verify the empty/skeleton states + a seeded run if fixtures provide one), Approvals resolve, Audit verify-chain (chip transitions + Overview checklist completes), Deployments tabs load. `read_console_messages` clean; screenshot each vs. the handoff.
- [ ] **Step 2:** `npm test && npm run build` green. Delete now-unused legacy components if fully orphaned (grep first): e.g. old `ui.tsx`/`AppShell` `Header` remnants used only by replaced pages — but KEEP anything `studio/edit` still imports (that's P2).
- [ ] **Step 3:** Med cap commit → `pyproject.toml` `0.10.2`; update `version.ts` VERSION to `0.10.2`.
```bash
git commit -m "feat(console): v0.10.2 — Operate screens (Runs, Approvals, Audit, Deployments)"
```

---

## Self-review checklist (fill during execution)
- Every endpoint in DESIGN §11 under Runs/Approvals/Deployments(detail)/Audit mapped to a task ✓
- Run polling advances nodes live (Task 1 Step 5) · approvals badge (Task 5) · audit-verify completes Overview checklist (Task 1/3) ✓
- No `any` in api.ts additions (confirm generated type names) · loading/empty/error each screen ✓
- Legacy pages replaced; `studio/edit` untouched (P2) ✓

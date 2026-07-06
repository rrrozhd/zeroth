# Console onboarding audit — guides & examples in the interface

**Date:** 2026-07-06 · **Branch:** feat/console-frontend · **Scope:** `frontend/app/**` (all 7 pages + shared components)

## Context

Follow-up to the 2026-07-06 positioning audit (17 quick wins). This pass asked one question:
*can a new user go from an empty deployment to a governed run without leaving the console or
reading external docs?* Answer before this change: no — every authoring surface started blank.

## Findings

| # | Finding | Location | Severity |
|---|---------|----------|----------|
| 1 | Studio empty state was a dead end ("No workflows yet.") — no templates, users start from a blank canvas | `studio/page.tsx` | High |
| 2 | Node config fields unexplained — `manifest_ref`, `connector_ref`, `model_provider` had no hints, examples, or per-type help | `NodeInspector.tsx`, `nodeMeta.tsx` | High |
| 3 | No in-console concept guide — draft→publish→deploy→run lifecycle, node semantics, and API usage undocumented in the UI | (missing page) | High |
| 4 | Overview had no getting-started path beyond the Connect hint | `page.tsx` | Medium |
| 5 | Empty states didn't explain how views get populated (Approvals, Audit, Runs list); run form gave no payload examples | `approvals/`, `audit/`, `runs/page.tsx` | Medium |

Positive findings (already good): friendly code-aware API error copy (`ApiErrorNote`), draft/published
banner in the editor, default JSON payload in the run form, `NotConnected` gating on data pages.

## Fixes applied (v0.4.2)

1. **Template gallery** — `lib/templates.ts` + gallery on the Studio page. Three templates:
   *Grounded Q&A (RAG)* (retrieval→agent), *Approval-gated action* (agent→human_approval→executable_unit),
   *Tool → Agent pipeline* (executable_unit→agent). One click creates a pre-wired editable draft and
   opens the editor. Configs validated against the real backend models (`AgentNodeData` etc.) and the
   full graphs through the actual PUT build path (`_build_node`/`_build_edge` + `Graph.model_validate`) — all pass.
2. **Editor empty canvas** — "Insert example graph" button inserts the RAG template client-side.
3. **Node inspector** — per-type help paragraph (from `NODE_META.help`) + per-field hint and example
   placeholder for every config key.
4. **`/guide` page** (new, in nav) — concepts, zero-to-run walkthrough, node type reference with
   example configs, curl quickstart for `POST /v1/runs`.
5. **Overview** — 4-step getting-started checklist linking to Studio/Runs/Approvals + guide pointer.
6. **Runs** — example payload chips (Question / Document / Structured task) + input-contract hint.
7. **Empty states** — Approvals/Audit/Runs now say how each view gets populated, with links.

## Verification

- `npm run build` green; all 9 routes including `/guide` export statically.
- Browser preview: guide, studio gallery, overview checklist, runs chips all render; chip click fills
  the textarea; zero console errors.
- Template payloads validated in-process against backend pydantic models and graph validation (see §Fixes 1).

## Known limitation (pre-existing, unrelated)

`tests/dispatch/test_worker.py` has timing-sensitive tests (`test_worker_recovers_orphaned_run`,
`test_worker_does_not_claim_more_runs_than_available_capacity`) that assert state after fixed
`asyncio.sleep(0.05–0.1)` windows. They fail deterministically under high machine load and block
the pre-commit hook. Worth a follow-up: replace fixed sleeps with polling-until-condition.

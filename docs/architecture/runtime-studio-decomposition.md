# Runtime & Studio Decomposition Plan (deferred)

**Status:** documented, deliberately NOT implemented. The v0.9.1 hardening pass
fixed behavior inside the existing file boundaries; this document is the map
for the structural split so it can be executed as its own change with its own
verification, instead of being mixed into security fixes.

## Hotspots (measured at v0.9.1)

| File | Lines | Shape |
|------|------:|-------|
| `src/zeroth/core/orchestrator/runtime.py` | 2,575 | one `RuntimeOrchestrator` class, ~48 methods; `_dispatch_node_inner` alone spans ~300 lines |
| `frontend/app/studio/edit/page.tsx` | 2,403 | one Next.js page module, ~75 functions/components |

Knowledge-graph queries to refresh these numbers and their blast radius:
`get_impact_radius` on `RuntimeOrchestrator._dispatch_node_inner`, and
`list_communities` for the `orchestrator` and `studio` clusters.

## Runtime seams (`runtime.py`)

Split along the phases `_dispatch_node_inner` already narrates, extracting
modules with explicit interfaces rather than reshuffling methods:

1. **Dispatch preparation** (`orchestrator/dispatch_prep.py`) — node lookup,
   input assembly, contract validation, enforcement-context construction
   (policy guard evaluation, capability set, timeout/approval overrides).
   Interface: `prepare_dispatch(graph, node, run, state) -> DispatchPlan`.
2. **Agent context** (`orchestrator/agent_context.py`) — the per-dispatch
   runner fork (`fork_for_dispatch`), provider wrapping (econ instrumentation),
   memory-resolver scoping, template/memory binding resolution, budget
   enforcer attachment. Interface: `build_agent_invocation(plan) -> Invocation`.
   This is the seam the v0.9.1 runner-isolation fix already hardened; the
   extraction must keep the isolation contract (no shared mutable runner state).
3. **Audit emission** (`orchestrator/audit_emit.py`) — NodeAuditRecord
   construction (tool_calls, memory_interactions, safety audits), signing, and
   the chain append through the DB coordination layer. Interface:
   `emit_node_audit(invocation, outcome) -> AuditRecordRef`.
4. **Parallel / subgraph coordination** (`orchestrator/coordination.py`) —
   fan-out branch spawning, merge strategies, subgraph executor calls, budget
   isolation across branches. Interface: `run_branches(plan) -> MergeResult`.

The orchestrator class then becomes a thin state machine over these modules
(claim → prepare → invoke → audit → advance).

## Studio seams (`page.tsx`)

Extract in dependency order, each piece taking its types with it:

1. **Editor state/controller** (`editorState.ts`) — nodes/edges state, undo
   history (`HISTORY_CAP`), clipboard, graph signature dirty-tracking, save
   pipeline. Pure reducer + hooks; no JSX.
2. **Canvas** (`EditorCanvas.tsx`) — React Flow wiring, node/edge type maps,
   connect/drop handlers, layout (`tidy`), selection.
3. **Deployment modal** (`DeployDialog.tsx`) — already a distinct component in
   the file; move it out with `canDeployWorkflow` (extracted in v0.9.1,
   `runEligibility.ts` — the pattern to follow).
4. **Run panel** (`RunPanel.tsx`) — run submission, health/served-deployment
   check (`canRunWorkflow`), status polling, per-node run states, thread field.
5. **Node inspector integration** (`inspectorBridge.ts`) — config patching,
   contract pickers, connector settings.

## Execution gates (phased, each independently shippable)

- **Gate 0 — characterization tests before any move.** Runtime: golden-path
  dispatch tests already exist (`tests/orchestrator`); add one
  characterization test per extracted interface asserting current behavior
  (inputs → audit record shape, branch merge results). Studio: extend the
  `runEligibility.ts` + Vitest pattern; add component render tests only for
  pieces as they are extracted.
- **Gate 1 — runtime seams 1+3** (prep, audit) — pure-function-heavy, lowest
  risk. Full suite + orchestrator checkpoint must stay green.
- **Gate 2 — runtime seams 2+4** (agent context, coordination) — touches the
  isolation contract; rerun the concurrency regression tests
  (`tests/orchestrator/test_dispatch_isolation*`).
- **Gate 3 — studio extraction** in the order above; `npm test -- --run` and
  `npm run build` after each step.

**Rollback points:** each gate is one commit; a failed gate reverts that
commit only. No cross-gate refactors.

**Non-goals:** no behavior changes, no new abstractions beyond the listed
interfaces, no renaming of public APIs or REST surfaces, no state-management
library adoption in the frontend.

**Target boundaries:** no runtime module over ~600 lines; no Studio component
file over ~500 lines; each extracted module owned by the subsystem that
consumes it (orchestrator modules under `zeroth.core.orchestrator`, Studio
pieces under `frontend/app/studio/edit/`).

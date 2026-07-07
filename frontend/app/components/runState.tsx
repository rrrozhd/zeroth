"use client";

import { createContext, useContext } from "react";
import type { NodeAuditRecord } from "@/app/lib/api";

// Per-node execution overlay for the canvas (n8n-style "run from canvas").
//
// Deliberately kept OUT of React Flow node data: `node.data` feeds the
// editor's autosave/undo structural signature (graphSig), so run state stored
// there would schedule spurious saves and pollute the history stack. The
// overlay flows through this context instead and never touches nodes/edges.

export type NodeRunState = {
  phase: "running" | "succeeded" | "failed" | "waiting";
  costUsd?: number | null;
};

export const RunStateContext = createContext<Record<string, NodeRunState>>({});

/** Run overlay for one canvas node; undefined = no overlay (default look). */
export function useNodeRunState(nodeId: string): NodeRunState | undefined {
  return useContext(RunStateContext)[nodeId];
}

const DONE = new Set(["completed", "succeeded", "ok"]);
const FAILED = new Set([
  "failed",
  "error",
  "terminated_by_policy",
  "terminated_by_loop_guard",
  "dead_letter",
]);

/** Phase of a single audit timeline entry. */
export function entryPhase(e: NodeAuditRecord): NodeRunState["phase"] {
  const s = (e.status ?? "").toLowerCase();
  if (DONE.has(s)) return "succeeded";
  if (FAILED.has(s)) return "failed";
  // Started but no terminal status yet: completed_at is the tie-breaker.
  if (e.completed_at) return e.error ? "failed" : "succeeded";
  return "running";
}

/** Fold timeline entries into per-node states. Later entries (retries) win;
 *  while the run is active, graph nodes with no entry yet show as waiting. */
export function deriveNodeStates(
  entries: NodeAuditRecord[],
  opts: { allNodeIds?: string[]; runActive?: boolean } = {},
): Record<string, NodeRunState> {
  const out: Record<string, NodeRunState> = {};
  for (const e of entries) {
    const prev = out[e.node_id];
    out[e.node_id] = {
      phase: entryPhase(e),
      costUsd: e.cost_usd ?? prev?.costUsd ?? null,
    };
  }
  if (opts.runActive && opts.allNodeIds) {
    for (const id of opts.allNodeIds) {
      if (!out[id]) out[id] = { phase: "waiting" };
    }
  }
  return out;
}

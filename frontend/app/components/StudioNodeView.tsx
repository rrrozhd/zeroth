"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { NodeGlyph } from "@/app/components/nodeMeta";
import { useNodeIssue, useNodeRunState, type NodeRunState } from "@/app/components/runState";
import { fmtUsd } from "@/app/components/ui";

export type Port = { id: string; type: string; direction: string; label: string };
export type StudioNodeData = {
  label: string;
  studioType: string;
  ports: Port[];
};

const PORT_TONE: Record<string, string> = {
  control: "!bg-amber-500",
  data: "!bg-blue-500",
};

// Run-overlay ring per execution phase; no entry = the default look below.
const RUN_RING: Record<NodeRunState["phase"], string> = {
  running: "ring-2 ring-accent/60 animate-pulse",
  succeeded: "ring-2 ring-emerald-500/50",
  failed: "ring-2 ring-red-500/50",
  waiting: "border-dashed ring-2 ring-zinc-400/40",
};

/** A graph node rendered with its typed ports as connectable handles:
 *  input ports on the left (targets), output ports on the right (sources). */
export function StudioNodeView({ id, data, selected }: NodeProps) {
  const d = data as StudioNodeData;
  const run = useNodeRunState(id);
  const issue = useNodeIssue(id);
  const inputs = d.ports.filter((p) => p.direction === "input");
  const outputs = d.ports.filter((p) => p.direction === "output");

  // The run ring replaces the selection ring while an overlay is painted;
  // selection keeps its accent border either way. Publish issues paint their
  // own ring, but a live run overlay outranks them.
  const issueRing =
    issue === "error" ? "ring-2 ring-red-500/60" : issue ? "ring-2 ring-amber-500/60" : "";
  const border = selected ? "border-accent" : "border-border hover:shadow-md";
  const ring = run ? RUN_RING[run.phase] : issueRing || (selected ? "ring-2 ring-accent/30" : "");

  return (
    <div
      className={`relative flex min-w-[180px] items-center gap-2.5 rounded-lg border bg-surface px-3 py-2.5 shadow-sm transition-shadow ${border} ${ring}`}
    >
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
        <NodeGlyph type={d.studioType} className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold leading-tight">{d.label}</div>
        <div className="text-[10px] uppercase tracking-wide text-muted">{d.studioType}</div>
      </div>

      {!run && issue && (
        <span
          title={issue === "error" ? "Publish blocked by this node" : "Publish warning"}
          className={`absolute -right-1.5 -top-1.5 grid h-4 w-4 place-items-center rounded-full text-[9px] font-bold text-white shadow-sm ${
            issue === "error" ? "bg-red-500" : "bg-amber-500"
          }`}
        >
          !
        </span>
      )}
      {(run?.phase === "succeeded" || run?.phase === "failed") && (
        <span
          title={run.phase === "succeeded" ? "Succeeded" : "Failed"}
          className={`absolute -right-1.5 -top-1.5 grid h-4 w-4 place-items-center rounded-full text-[9px] font-bold text-white shadow-sm ${
            run.phase === "succeeded" ? "bg-emerald-500" : "bg-red-500"
          }`}
        >
          {run.phase === "succeeded" ? "✓" : "✕"}
        </span>
      )}
      {run?.costUsd != null && (
        <span
          title="Cost for this run"
          className="absolute -bottom-2 right-2 rounded-full border border-border bg-surface px-1.5 text-[9px] leading-4 text-muted shadow-sm"
        >
          {fmtUsd(run.costUsd)}
        </span>
      )}

      {inputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="target"
          position={Position.Left}
          title={p.label}
          aria-label={p.label}
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ top: `${((i + 1) / (inputs.length + 1)) * 100}%` }}
        />
      ))}
      {outputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="source"
          position={Position.Right}
          title={p.label}
          aria-label={p.label}
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ top: `${((i + 1) / (outputs.length + 1)) * 100}%` }}
        />
      ))}
    </div>
  );
}

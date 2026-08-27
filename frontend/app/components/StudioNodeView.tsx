"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { NodeGlyph, NODE_META } from "@/app/components/nodeMeta";
import { useNodeIssue, useNodeRunState, type NodeRunState } from "@/app/components/runState";
import { fmtUsd } from "@/app/components/ui";

export type Port = { id: string; type: string; direction: string; label: string };
export type StudioNodeData = {
  label: string;
  studioType: string;
  ports: Port[];
  config?: Record<string, unknown>;
};

const PORT_TONE: Record<string, string> = {
  control: "!bg-amber-500",
  data: "!bg-blue-500",
  tool: "!bg-violet-500",
};

const BASE_SHADOW = "0 1px 4px rgba(22,21,31,.05)";

// Ring color per execution phase; no entry = no run overlay.
const RUN_RING: Record<NodeRunState["phase"], string> = {
  running: "var(--accent)",
  succeeded: "var(--success)",
  failed: "var(--danger)",
  waiting: "var(--neutral)",
};

/** A graph node rendered with its typed ports as connectable handles:
 *  input ports on the left (targets), output ports on the right (sources).
 *  Tool ports are a separate set of edges — they render on the bottom
 *  (the agent's Tools source) and top (a unit's Tool target) instead. */
export function StudioNodeView({ id, data, selected }: NodeProps) {
  const d = data as StudioNodeData;
  const run = useNodeRunState(id);
  const issue = useNodeIssue(id);
  const inputs = d.ports.filter((p) => p.direction === "input" && p.type !== "tool");
  const outputs = d.ports.filter((p) => p.direction === "output" && p.type !== "tool");
  const toolInputs = d.ports.filter((p) => p.direction === "input" && p.type === "tool");
  const toolOutputs = d.ports.filter((p) => p.direction === "output" && p.type === "tool");

  const isLoop = d.studioType === "loop";
  const isIf = d.studioType === "if";
  const maxRetries = Number(d.config?.max_retries ?? 3);
  const sub = isLoop
    ? `1 attempt + ${maxRetries} ${maxRetries === 1 ? "retry" : "retries"}`
    : isIf
      ? String(d.config?.expression || "Set a condition")
    : NODE_META[d.studioType]?.blurb;

  // Ring precedence: a live run overlay outranks a publish issue, which
  // outranks the selection ring. The base drop shadow is always painted.
  const issueRing = issue === "error" ? "var(--danger)" : issue ? "var(--warning)" : "";
  const ringColor = run
    ? RUN_RING[run.phase]
    : issueRing || (selected ? "var(--accent)" : "");
  const borderColor = selected ? "var(--accent)" : "var(--hair-strong)";
  const boxShadow = ringColor ? `${BASE_SHADOW}, 0 0 0 2px ${ringColor}` : BASE_SHADOW;

  return (
    <div
      className={`studio-node-card relative${isLoop ? " is-loop" : ""}${isIf ? " is-if" : ""} ${run?.phase === "running" ? "z-pulse" : ""}`}
      data-evidence-id={`studio.node-content.${id}`}
      style={{
        borderColor,
        borderStyle: run?.phase === "waiting" ? "dashed" : "solid",
        borderRadius: d.studioType === "entrypoint" ? "20px 10px 10px 20px" : undefined,
        boxShadow,
      }}
    >
      <span className="studio-node-glyph">
        <NodeGlyph type={d.studioType} className="h-[18px] w-[18px]" />
      </span>
      <span className="studio-node-copy">
        <strong className="studio-node-title">{d.label}</strong>
        {sub && <span className="studio-node-subtitle">{sub}</span>}
      </span>
      {(isLoop || isIf) && (
        <span className="studio-control-port-labels" aria-hidden="true">
          {outputs.map((port) => <span key={port.id}>{port.label}</span>)}
        </span>
      )}

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
          className="absolute -bottom-2 right-2 rounded-full px-1.5 text-[9px] leading-4 shadow-sm"
          style={{
            background: "var(--bg-raised-2)",
            border: "1px solid var(--hair-strong)",
            color: "var(--text-muted)",
          }}
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
          role="button"
          tabIndex={0}
          data-evidence-id={`studio.port.${id}.${p.id}.target`}
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
          role="button"
          tabIndex={0}
          data-evidence-id={`studio.port.${id}.${p.id}.source`}
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ top: `${((i + 1) / (outputs.length + 1)) * 100}%` }}
        />
      ))}
      {toolInputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="target"
          position={Position.Top}
          title="Tool — connect from an agent's bottom handle"
          aria-label={p.label}
          role="button"
          tabIndex={0}
          data-evidence-id={`studio.port.${id}.${p.id}.tool-target`}
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ left: `${((i + 1) / (toolInputs.length + 1)) * 100}%` }}
        />
      ))}
      {toolOutputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="source"
          position={Position.Bottom}
          title="Tools — drag onto a Code or Executable Unit node to attach it as a callable tool"
          aria-label={p.label}
          role="button"
          tabIndex={0}
          data-evidence-id={`studio.port.${id}.${p.id}.tool-source`}
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ left: `${((i + 1) / (toolOutputs.length + 1)) * 100}%` }}
        />
      ))}
    </div>
  );
}

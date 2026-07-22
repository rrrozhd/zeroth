"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { nodeMetaColor, NODE_META } from "@/app/components/nodeMeta";
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
  tool: "!bg-violet-500",
};

// Handoff card drop shadow (0 4px 16px rgba(0,0,0,0.35)); a 2px ring is stacked
// on top of it via box-shadow for selection / run / publish-issue overlays.
const BASE_SHADOW = "0 4px 16px rgba(0,0,0,0.35)";

// Ring color per execution phase; no entry = no run overlay.
const RUN_RING: Record<NodeRunState["phase"], string> = {
  running: "rgba(94,234,212,0.6)", // --accent
  succeeded: "rgba(134,239,172,0.55)", // --success
  failed: "rgba(248,113,113,0.55)", // --danger
  waiting: "rgba(163,173,194,0.45)", // --neutral
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

  const typeColor = nodeMetaColor(d.studioType);
  const sub = NODE_META[d.studioType]?.blurb;

  // Ring precedence: a live run overlay outranks a publish issue, which
  // outranks the selection ring. The base drop shadow is always painted.
  const issueRing = issue === "error" ? "rgba(248,113,113,0.6)" : issue ? "rgba(252,211,77,0.6)" : "";
  const ringColor = run
    ? RUN_RING[run.phase]
    : issueRing || (selected ? "rgba(94,234,212,0.35)" : "");
  const borderColor = selected ? "rgba(94,234,212,0.6)" : "rgba(255,255,255,0.1)";
  const boxShadow = ringColor ? `${BASE_SHADOW}, 0 0 0 2px ${ringColor}` : BASE_SHADOW;

  return (
    <div
      className={`relative ${run?.phase === "running" ? "z-pulse" : ""}`}
      style={{
        width: 178,
        background: "var(--bg-raised)",
        border: `1px solid ${borderColor}`,
        borderStyle: run?.phase === "waiting" ? "dashed" : "solid",
        borderRadius: 8,
        boxShadow,
        cursor: "grab",
        padding: "10px 12px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 5 }}>
        <span
          aria-hidden
          style={{
            width: 7,
            height: 7,
            flexShrink: 0,
            borderRadius: 2,
            background: typeColor,
          }}
        />
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 9,
            fontWeight: 600,
            letterSpacing: "0.09em",
            textTransform: "uppercase",
            color: "var(--text-muted)",
          }}
        >
          {d.studioType}
        </span>
      </div>
      <div
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 12.5,
          fontWeight: 600,
          lineHeight: 1.25,
          color: "var(--text-primary)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {d.label}
      </div>
      {sub && (
        <div
          style={{
            marginTop: 2,
            fontSize: 10.5,
            color: "var(--text-faint)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {sub}
        </div>
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
      {toolInputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="target"
          position={Position.Top}
          title="Tool — connect from an agent's bottom handle"
          aria-label={p.label}
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
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ left: `${((i + 1) / (toolOutputs.length + 1)) * 100}%` }}
        />
      ))}
    </div>
  );
}

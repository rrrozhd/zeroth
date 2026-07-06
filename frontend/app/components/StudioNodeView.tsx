"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { NodeGlyph } from "@/app/components/nodeMeta";

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

/** A graph node rendered with its typed ports as connectable handles:
 *  input ports on the left (targets), output ports on the right (sources). */
export function StudioNodeView({ data, selected }: NodeProps) {
  const d = data as StudioNodeData;
  const inputs = d.ports.filter((p) => p.direction === "input");
  const outputs = d.ports.filter((p) => p.direction === "output");

  return (
    <div
      className={`flex min-w-[180px] items-center gap-2.5 rounded-lg border bg-surface px-3 py-2.5 shadow-sm transition-shadow ${
        selected
          ? "border-accent ring-2 ring-accent/30"
          : "border-border hover:shadow-md"
      }`}
    >
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
        <NodeGlyph type={d.studioType} className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold leading-tight">{d.label}</div>
        <div className="text-[10px] uppercase tracking-wide text-muted">{d.studioType}</div>
      </div>

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

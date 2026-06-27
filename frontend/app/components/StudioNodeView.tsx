"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";

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
      className={`min-w-[150px] rounded-md border bg-white px-3 py-2 shadow-sm dark:bg-zinc-900 ${
        selected
          ? "border-zinc-900 ring-1 ring-zinc-900 dark:border-zinc-100 dark:ring-zinc-100"
          : "border-zinc-300 dark:border-zinc-700"
      }`}
    >
      <div className="text-sm font-semibold leading-tight">{d.label}</div>
      <div className="text-[10px] uppercase tracking-wide text-zinc-400">
        {d.studioType}
      </div>

      {inputs.map((p, i) => (
        <Handle
          key={p.id}
          id={p.id}
          type="target"
          position={Position.Left}
          title={p.label}
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
          className={`h-2.5 w-2.5 ${PORT_TONE[p.type] ?? "!bg-zinc-400"}`}
          style={{ top: `${((i + 1) / (outputs.length + 1)) * 100}%` }}
        />
      ))}
    </div>
  );
}

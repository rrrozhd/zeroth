"use client";

// Per-type config forms for the executable node types. Field keys map directly
// to the backend *NodeData models; values live in the canvas node's
// data.config and round-trip through PUT /workflows/{id}.

type Field = {
  key: string;
  label: string;
  kind: "text" | "textarea" | "number" | "select";
  required?: boolean;
  options?: string[];
  placeholder?: string;
};

export const FIELD_SPECS: Record<string, Field[]> = {
  agent: [
    { key: "instruction", label: "Instruction", kind: "textarea", required: true },
    {
      key: "model_provider",
      label: "Model provider",
      kind: "text",
      required: true,
      placeholder: "openai/gpt-4o",
    },
  ],
  executable_unit: [
    { key: "manifest_ref", label: "Manifest ref", kind: "text", required: true },
    {
      key: "execution_mode",
      label: "Execution mode",
      kind: "select",
      required: true,
      options: ["native", "wrapped_command", "project"],
    },
  ],
  human_approval: [
    { key: "sla_timeout_seconds", label: "SLA timeout (seconds)", kind: "number" },
  ],
  retrieval: [
    { key: "connector_ref", label: "Connector ref", kind: "text", required: true },
    { key: "top_k", label: "Top K", kind: "number", placeholder: "5" },
  ],
  subgraph: [
    { key: "graph_ref", label: "Graph ref", kind: "text", required: true },
    { key: "version", label: "Version", kind: "number" },
  ],
};

// Initial config for a freshly added node — pre-fills required keys so the first
// save validates (e.g. execution_mode must be a valid enum value).
export const DEFAULT_CONFIG: Record<string, Record<string, unknown>> = {
  agent: { instruction: "", model_provider: "" },
  executable_unit: { manifest_ref: "", execution_mode: "native" },
  human_approval: {},
  retrieval: { connector_ref: "" },
  subgraph: { graph_ref: "" },
};

export function NodeInspector({
  studioType,
  label,
  config,
  onLabelChange,
  onConfigChange,
}: {
  studioType: string;
  label: string;
  config: Record<string, unknown>;
  onLabelChange: (v: string) => void;
  onConfigChange: (next: Record<string, unknown>) => void;
}) {
  const fields = FIELD_SPECS[studioType] ?? [];

  function setField(key: string, raw: string, kind: Field["kind"]) {
    const next = { ...config };
    if (raw === "") {
      delete next[key];
    } else {
      next[key] = kind === "number" ? Number(raw) : raw;
    }
    onConfigChange(next);
  }

  const inputCls =
    "w-full rounded-md border border-zinc-300 px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-900";

  return (
    <div className="space-y-3 border-t border-zinc-200 pt-3 dark:border-zinc-800">
      <div className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
        {studioType}
      </div>

      <label className="block text-xs">
        <span className="mb-1 block text-zinc-500">Label</span>
        <input value={label} onChange={(e) => onLabelChange(e.target.value)} className={inputCls} />
      </label>

      {fields.map((f) => {
        const value = config[f.key];
        const str = value === undefined || value === null ? "" : String(value);
        return (
          <label key={f.key} className="block text-xs">
            <span className="mb-1 block text-zinc-500">
              {f.label}
              {f.required && <span className="text-amber-600"> *</span>}
            </span>
            {f.kind === "textarea" ? (
              <textarea
                value={str}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                rows={3}
                className={inputCls}
              />
            ) : f.kind === "select" ? (
              <select
                value={str}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                className={inputCls}
              >
                {(f.options ?? []).map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={str}
                type={f.kind === "number" ? "number" : "text"}
                placeholder={f.placeholder}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                className={inputCls}
              />
            )}
          </label>
        );
      })}

      <p className="text-[10px] text-zinc-400">
        Select a node/edge and press Backspace to delete. <span className="text-amber-600">*</span>{" "}
        required to publish.
      </p>
    </div>
  );
}

"use client";

import { NODE_META } from "@/app/components/nodeMeta";
import { fieldInput } from "@/app/components/ui";

// Per-type config forms for the executable node types. Field keys map directly
// to the backend *NodeData models; values live in the canvas node's
// data.config and round-trip through PUT /workflows/{id}.

type Field = {
  key: string;
  label: string;
  kind: "text" | "textarea" | "number" | "select";
  required?: boolean;
  options?: string[];
  /** Key into the dynamicOptions prop — options fetched at runtime (e.g. the
      deployment's registered memory connectors). Falls back to a text input
      while no dynamic options are available. */
  optionsFrom?: string;
  placeholder?: string;
  hint?: string;
};

export const FIELD_SPECS: Record<string, Field[]> = {
  agent: [
    {
      key: "instruction",
      label: "Instruction",
      kind: "textarea",
      required: true,
      placeholder: "Summarize the incoming document in five bullet points…",
      hint: "What this step should do. The agent receives the upstream node's output as context.",
    },
    {
      key: "model_provider",
      label: "Model provider",
      kind: "text",
      required: true,
      placeholder: "openai/gpt-4o",
      hint: "provider/model, e.g. openai/gpt-4o or anthropic/claude-sonnet-5.",
    },
  ],
  executable_unit: [
    {
      key: "manifest_ref",
      label: "Manifest ref",
      kind: "select",
      optionsFrom: "manifests",
      required: true,
      placeholder: "tools/my_tool",
      hint: "Registered code unit to run — the list comes from this deployment. Code is registered in Python (the medium-code path), not authored here.",
    },
    {
      key: "execution_mode",
      label: "Execution mode",
      kind: "select",
      required: true,
      options: ["native", "wrapped_command", "project"],
      hint: "native = in-process Python; wrapped_command = shell command; project = project runner.",
    },
  ],
  human_approval: [
    {
      key: "sla_timeout_seconds",
      label: "SLA timeout (seconds)",
      kind: "number",
      placeholder: "86400",
      hint: "Escalate if the gate is still unresolved after this long. Blank = no SLA.",
    },
  ],
  retrieval: [
    {
      key: "connector_ref",
      label: "Connector",
      kind: "select",
      optionsFrom: "connectors",
      required: true,
      placeholder: "key_value",
      hint: "The registered memory connector to query — the list comes from this deployment. Manage them on the Connectors page.",
    },
    {
      key: "query_key",
      label: "Query key",
      kind: "text",
      placeholder: "query",
      hint: "Input field holding the search text. Default: query.",
    },
    {
      key: "top_k",
      label: "Top K",
      kind: "number",
      placeholder: "5",
      hint: "How many matches to pass downstream.",
    },
    {
      key: "scope",
      label: "Scope",
      kind: "select",
      options: ["shared", "thread", "run"],
      hint: "shared = whole deployment; thread = this conversation; run = this run only.",
    },
    {
      key: "as_name",
      label: "Output name",
      kind: "text",
      placeholder: "retrieved",
      hint: "Payload field the chunks are attached under. Default: retrieved.",
    },
  ],
  subgraph: [
    {
      key: "graph_ref",
      label: "Graph ref",
      kind: "text",
      required: true,
      placeholder: "my-subflow",
      hint: "The published graph to invoke as this step.",
    },
    {
      key: "version",
      label: "Version",
      kind: "number",
      hint: "Pin a specific version. Blank = latest published.",
    },
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
  readOnly = false,
  dynamicOptions,
}: {
  studioType: string;
  label: string;
  config: Record<string, unknown>;
  onLabelChange: (v: string) => void;
  onConfigChange: (next: Record<string, unknown>) => void;
  readOnly?: boolean;
  /** Runtime-fetched option lists, keyed by Field.optionsFrom (e.g. connectors). */
  dynamicOptions?: Record<string, string[] | undefined>;
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

  const inputCls = `${fieldInput} disabled:opacity-60`;

  return (
    <div className="space-y-4">
      {NODE_META[studioType]?.help && (
        <p className="rounded-lg bg-accent/[0.06] px-3 py-2 text-xs leading-relaxed text-muted">
          {NODE_META[studioType].help}
        </p>
      )}

      <label className="block text-sm">
        <span className="mb-1 block font-medium">Label</span>
        <input
          value={label}
          disabled={readOnly}
          onChange={(e) => onLabelChange(e.target.value)}
          className={inputCls}
        />
      </label>

      {fields.map((f) => {
        const value = config[f.key];
        const str = value === undefined || value === null ? "" : String(value);
        // Dynamic selects degrade to a text input while their options haven't
        // loaded (or the API predates the endpoint) so the field stays editable.
        let options = f.options;
        if (f.optionsFrom) options = dynamicOptions?.[f.optionsFrom];
        const asSelect =
          f.kind === "select" && (f.optionsFrom ? (options?.length ?? 0) > 0 : true);
        // Keep a saved value that isn't in the list selectable rather than
        // silently coercing it to the first option.
        if (asSelect && str && !(options ?? []).includes(str)) {
          options = [str, ...(options ?? [])];
        }
        return (
          <label key={f.key} className="block text-sm">
            <span className="mb-1 block font-medium">
              {f.label}
              {f.required && <span className="text-red-600 dark:text-red-400"> *</span>}
            </span>
            {f.kind === "textarea" ? (
              <textarea
                value={str}
                placeholder={f.placeholder}
                disabled={readOnly}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                rows={4}
                className={inputCls}
              />
            ) : asSelect ? (
              <select
                value={str}
                disabled={readOnly}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                className={inputCls}
              >
                {f.required ? (
                  !str && (
                    <option value="" disabled>
                      Select…
                    </option>
                  )
                ) : (
                  <option value="">(default)</option>
                )}
                {(options ?? []).map((o) => (
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
                disabled={readOnly}
                onChange={(e) => setField(f.key, e.target.value, f.kind)}
                className={inputCls}
              />
            )}
            {f.hint && <span className="mt-1 block text-xs font-normal text-muted">{f.hint}</span>}
          </label>
        );
      })}

      {fields.some((f) => f.required) && (
        <p className="text-xs text-muted">
          <span className="text-red-600 dark:text-red-400">*</span> required for the graph to publish.
        </p>
      )}
    </div>
  );
}

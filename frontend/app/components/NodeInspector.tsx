"use client";

import dynamic from "next/dynamic";
import { useRef, useState } from "react";
import { InlineConnectorSettings } from "@/app/components/ConnectorInline";
import { NODE_META } from "@/app/components/nodeMeta";
import { fieldInput } from "@/app/components/ui";
import { createContract, errMsg } from "@/app/lib/api";
import type { ConnectorSummary } from "@/app/lib/api";

// CodeMirror only loads when a code node's inspector actually opens — it has
// no business in the other pages' bundles.
const CodeEditor = dynamic(() => import("@/app/components/CodeEditor"), {
  ssr: false,
  loading: () => (
    <div className="h-[280px] animate-pulse rounded-lg border border-border bg-zinc-100 dark:bg-zinc-800/60" />
  ),
});

// Per-type config forms for the executable node types. Field keys map directly
// to the backend *NodeData models; values live in the canvas node's
// data.config and round-trip through PUT /workflows/{id}.

type Field = {
  key: string;
  label: string;
  kind: "text" | "textarea" | "number" | "select" | "code" | "checkbox";
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
  entrypoint: [],
  code: [
    {
      key: "inline_source",
      label: "Code",
      kind: "code",
      required: true,
      hint: "Python, standard library only. Reads the upstream payload as JSON on stdin; whatever it writes to stdout as JSON flows downstream. Sandbox-executed; frozen and content-hashed at publish.",
    },
    {
      key: "timeout_seconds",
      label: "Timeout (seconds)",
      kind: "number",
      placeholder: "30",
      hint: "Kill the process if it runs longer. Blank = platform default.",
    },
  ],
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
    {
      key: "input_messages_key",
      label: "Messages input key",
      kind: "text",
      placeholder: "messages",
      hint: "Optional. Name an input field holding a list of chat messages ({role: human | ai | tool, content}) — they become real conversation turns instead of one JSON blob. Blank = plain payload input.",
    },
    {
      key: "persist_conversation",
      label: "Persist conversation",
      kind: "checkbox",
      hint: "Store the conversation in thread state: stored turns replay before incoming ones, and each run appends the new turns plus the agent's reply. Callers continue a conversation by submitting the same thread_id — they only send what's new. Requires a messages input key.",
    },
    {
      key: "conversation_max_turns",
      label: "Max conversation turns",
      kind: "number",
      placeholder: "50",
      hint: "Cap on turns kept and replayed when persisting. Blank = keep everything.",
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
      hint: "The registered memory connector to query. Adjust its backend settings right below, or add a new one without leaving this dialog.",
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

// Starter source for a fresh code node: a working identity transform that
// documents the stdin/stdout contract by example.
const CODE_STARTER = `import json
import sys

data = json.load(sys.stdin)

# Transform the upstream payload here.
result = {"echo": data}

json.dump(result, sys.stdout)
`;

// Initial config for a freshly added node — pre-fills required keys so the first
// save validates (e.g. execution_mode must be a valid enum value).
export const DEFAULT_CONFIG: Record<string, Record<string, unknown>> = {
  agent: { instruction: "", model_provider: "" },
  entrypoint: {},
  code: { inline_source: CODE_STARTER, execution_mode: "inline" },
  executable_unit: { manifest_ref: "", execution_mode: "native" },
  human_approval: {},
  retrieval: { connector_ref: "" },
  subgraph: { graph_ref: "" },
};

export function NodeInspector({
  studioType,
  label,
  config,
  inputContractRef,
  outputContractRef,
  contractOptions,
  toolTargets,
  onLabelChange,
  onConfigChange,
  onContractRefChange,
  readOnly = false,
  dynamicOptions,
  connectors,
  onConnectorsChanged,
  onContractsChanged,
}: {
  studioType: string;
  label: string;
  config: Record<string, unknown>;
  /** Contract bindings — node-level fields (NodeBase), not config keys. */
  inputContractRef?: string | null;
  outputContractRef?: string | null;
  /** Registered contract names from GET /api/studio/v1/contracts. */
  contractOptions?: string[];
  /** Units attached to this agent via tool edges (the bottom Tools handle). */
  toolTargets?: { id: string; label: string }[];
  onLabelChange: (v: string) => void;
  onConfigChange: (next: Record<string, unknown>) => void;
  onContractRefChange?: (which: "input" | "output", ref: string | null) => void;
  readOnly?: boolean;
  /** Runtime-fetched option lists, keyed by Field.optionsFrom (e.g. connectors). */
  dynamicOptions?: Record<string, string[] | undefined>;
  /** Full connector summaries — enables the inline settings panel under
      connector selects (edit params, test, create) without leaving the dialog. */
  connectors?: ConnectorSummary[];
  /** Re-fetch connectors after an inline create/update. */
  onConnectorsChanged?: () => void | Promise<void>;
  /** Re-fetch contracts after an inline create. */
  onContractsChanged?: () => void | Promise<void>;
}) {
  const fields = FIELD_SPECS[studioType] ?? [];

  // Async callers (the inline connector panel's create flow) invoke setField
  // after awaited network calls — spread the latest config, not the click-time
  // snapshot, so edits made to other fields in the meantime survive.
  const configRef = useRef(config);
  configRef.current = config;

  function setField(key: string, raw: string, kind: Field["kind"]) {
    const next = { ...configRef.current };
    if (raw === "" && kind !== "code") {
      // Cleared code stays an explicit "" — the backend keeps the inline
      // invariant on drafts and publish is the emptiness gate.
      delete next[key];
    } else {
      next[key] = kind === "number" ? Number(raw) : raw;
    }
    onConfigChange(next);
  }

  function setBoolField(key: string, checked: boolean) {
    const next = { ...configRef.current };
    // Unchecked = backend default (false) — drop the key like cleared text.
    if (checked) next[key] = true;
    else delete next[key];
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
          <div key={f.key}>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">
              {f.label}
              {f.required && <span className="text-red-600 dark:text-red-400"> *</span>}
            </span>
            {f.kind === "checkbox" ? (
              <span className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={Boolean(value)}
                  disabled={readOnly}
                  onChange={(e) => setBoolField(f.key, e.target.checked)}
                />
                <span className="text-xs text-muted">{value ? "On" : "Off"}</span>
              </span>
            ) : f.kind === "code" ? (
              <CodeEditor
                value={str}
                readOnly={readOnly}
                onChange={(v) => setField(f.key, v, f.kind)}
              />
            ) : f.kind === "textarea" ? (
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
          {/* Settings for the selected connector, editable in place — the
              panel lives outside the <label> so its buttons don't retarget
              clicks to the select. */}
          {f.optionsFrom === "connectors" && connectors && (
            <InlineConnectorSettings
              selectedRef={str}
              connectors={connectors}
              readOnly={readOnly}
              onSelectRef={(ref) => setField(f.key, ref, f.kind)}
              onChanged={onConnectorsChanged}
            />
          )}
          </div>
        );
      })}

      {studioType === "agent" && toolTargets && (
        <AgentToolBindings
          targets={toolTargets}
          bindings={(config.tool_bindings as ToolBinding[] | undefined) ?? []}
          readOnly={readOnly}
          inputCls={inputCls}
          onChange={(next) => onConfigChange({ ...configRef.current, tool_bindings: next })}
        />
      )}

      {onContractRefChange && studioType === "entrypoint" && (
        <fieldset className="space-y-4 border-t border-border pt-4">
          <legend className="sr-only">Workflow input contract</legend>
          <ContractPicker
            label="Workflow input contract"
            hint="The shape callers must send to POST /v1/runs — validated at the door. Required to publish."
            value={inputContractRef ?? null}
            options={contractOptions ?? []}
            readOnly={readOnly}
            inputCls={inputCls}
            onChange={(ref) => onContractRefChange("input", ref)}
            onContractsChanged={onContractsChanged}
          />
        </fieldset>
      )}

      {onContractRefChange && studioType !== "entrypoint" && (
        <fieldset className="space-y-4 border-t border-border pt-4">
          <legend className="sr-only">Contract bindings</legend>
          <ContractPicker
            label="Input contract"
            hint="Validates the payload entering this node. Blank = unvalidated."
            value={inputContractRef ?? null}
            options={contractOptions ?? []}
            readOnly={readOnly}
            inputCls={inputCls}
            onChange={(ref) => onContractRefChange("input", ref)}
            onContractsChanged={onContractsChanged}
          />
          <ContractPicker
            label="Output contract"
            hint="Validates what this node emits downstream. Blank = unvalidated."
            value={outputContractRef ?? null}
            options={contractOptions ?? []}
            readOnly={readOnly}
            inputCls={inputCls}
            onChange={(ref) => onContractRefChange("output", ref)}
            onContractsChanged={onContractsChanged}
          />
        </fieldset>
      )}

      {fields.some((f) => f.required) && (
        <p className="text-xs text-muted">
          <span className="text-red-600 dark:text-red-400">*</span> required for the graph to publish.
        </p>
      )}
    </div>
  );
}

// Tool bindings authored per attached unit. Field keys map to the backend
// AgentToolBinding model; the list lives in the agent's config.tool_bindings.

type ToolArg = { name: string; type: string; description: string; required: boolean };

type ToolBinding = {
  target_node_id: string;
  name: string;
  description: string;
  arguments: ToolArg[];
};

const TOOL_ARG_TYPES = ["string", "number", "integer", "boolean", "object", "array"];

function AgentToolBindings({
  targets,
  bindings,
  readOnly,
  inputCls,
  onChange,
}: {
  targets: { id: string; label: string }[];
  bindings: ToolBinding[];
  readOnly: boolean;
  inputCls: string;
  onChange: (next: ToolBinding[]) => void;
}) {
  // Bindings whose unit is no longer attached — publish will reject them, so
  // surface them here with a one-click cleanup instead of failing silently.
  const orphans = bindings.filter((b) => !targets.some((t) => t.id === b.target_node_id));

  function upsert(targetId: string, patch: Partial<ToolBinding>) {
    const exists = bindings.some((b) => b.target_node_id === targetId);
    onChange(
      exists
        ? bindings.map((b) => (b.target_node_id === targetId ? { ...b, ...patch } : b))
        : [
            ...bindings,
            { target_node_id: targetId, name: "", description: "", arguments: [], ...patch },
          ],
    );
  }

  function patchArgs(targetId: string, args: ToolArg[]) {
    upsert(targetId, { arguments: args });
  }

  return (
    <fieldset className="space-y-3 border-t border-border pt-4">
      <legend className="sr-only">Attached tools</legend>
      <div>
        <span className="block text-sm font-medium">Attached tools</span>
        <span className="mt-0.5 block text-xs text-muted">
          Units connected to the Tools handle (bottom of the node). The agent can call each
          one — give it a distinct name, a description, and describe every argument; the
          model sees only what you write here.
        </span>
      </div>

      {targets.length === 0 && orphans.length === 0 && (
        <p className="rounded-lg bg-accent/[0.06] px-3 py-2 text-xs text-muted">
          Nothing attached yet. Drag from the violet handle on the bottom of this node to a
          Code or Executable Unit node to attach it as a callable tool.
        </p>
      )}

      {targets.map((t) => {
        const b = bindings.find((x) => x.target_node_id === t.id);
        const args = b?.arguments ?? [];
        return (
          <div key={t.id} className="space-y-2 rounded-lg border border-border p-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-xs font-semibold">{t.label}</span>
              <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted">
                {t.id}
              </span>
            </div>
            <label className="block text-xs">
              <span className="mb-1 block font-medium">
                Tool name<span className="text-red-600 dark:text-red-400"> *</span>
              </span>
              <input
                value={b?.name ?? ""}
                placeholder="lookup_customer"
                disabled={readOnly}
                onChange={(e) => upsert(t.id, { name: e.target.value })}
                className={`${inputCls} font-mono`}
              />
              <span className="mt-1 block font-normal text-muted">
                Letters, digits, _ or - only. Must be unique across this agent&apos;s tools.
              </span>
            </label>
            <label className="block text-xs">
              <span className="mb-1 block font-medium">
                Description<span className="text-red-600 dark:text-red-400"> *</span>
              </span>
              <textarea
                value={b?.description ?? ""}
                placeholder="Looks up a customer record by id and returns their profile."
                disabled={readOnly}
                onChange={(e) => upsert(t.id, { description: e.target.value })}
                rows={2}
                className={inputCls}
              />
            </label>
            <div className="space-y-1.5">
              <span className="block text-xs font-medium">Arguments</span>
              {args.map((a, i) => (
                <div key={i} className="space-y-1.5 rounded border border-border p-2">
                  <div className="flex gap-1.5">
                    <input
                      value={a.name}
                      placeholder="name"
                      aria-label="Argument name"
                      disabled={readOnly}
                      onChange={(e) =>
                        patchArgs(
                          t.id,
                          args.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)),
                        )
                      }
                      className={`${inputCls} font-mono`}
                    />
                    <select
                      value={a.type}
                      aria-label="Argument type"
                      disabled={readOnly}
                      onChange={(e) =>
                        patchArgs(
                          t.id,
                          args.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)),
                        )
                      }
                      className={`${inputCls} w-28`}
                    >
                      {TOOL_ARG_TYPES.map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                    {!readOnly && (
                      <button
                        type="button"
                        aria-label="Remove argument"
                        onClick={() => patchArgs(t.id, args.filter((_, j) => j !== i))}
                        className="shrink-0 rounded-lg border border-border px-2 text-xs text-muted hover:text-red-600"
                      >
                        ✕
                      </button>
                    )}
                  </div>
                  <input
                    value={a.description}
                    placeholder="What this argument means (required)"
                    aria-label="Argument description"
                    disabled={readOnly}
                    onChange={(e) =>
                      patchArgs(
                        t.id,
                        args.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)),
                      )
                    }
                    className={inputCls}
                  />
                  <label className="flex items-center gap-1.5 text-xs text-muted">
                    <input
                      type="checkbox"
                      checked={a.required}
                      disabled={readOnly}
                      onChange={(e) =>
                        patchArgs(
                          t.id,
                          args.map((x, j) => (j === i ? { ...x, required: e.target.checked } : x)),
                        )
                      }
                    />
                    Required
                  </label>
                </div>
              ))}
              {!readOnly && (
                <button
                  type="button"
                  onClick={() =>
                    patchArgs(t.id, [
                      ...args,
                      { name: "", type: "string", description: "", required: true },
                    ])
                  }
                  className="text-xs font-medium text-accent hover:underline"
                >
                  + Add argument
                </button>
              )}
            </div>
          </div>
        );
      })}

      {orphans.map((b) => (
        <div
          key={b.target_node_id}
          className="flex items-center justify-between gap-2 rounded-lg border border-amber-500/40 bg-amber-500/[0.07] p-2.5 text-xs"
        >
          <span className="min-w-0 truncate">
            <span className="font-semibold">{b.name || "(unnamed tool)"}</span> points at{" "}
            <span className="font-mono">{b.target_node_id}</span>, which is no longer attached.
          </span>
          {!readOnly && (
            <button
              type="button"
              onClick={() =>
                onChange(bindings.filter((x) => x.target_node_id !== b.target_node_id))
              }
              className="shrink-0 rounded-lg border border-border px-2 py-0.5 font-medium hover:text-red-600"
            >
              Remove
            </button>
          )}
        </div>
      ))}
    </fieldset>
  );
}

// Contract dropdown fed by the deployment's contract registry; degrades to a
// text input while the registry is empty/unreachable (same as dynamic selects).
const CONTRACT_SCHEMA_STARTER = `{
  "type": "object",
  "properties": {
    "field": { "type": "string" }
  },
  "required": ["field"],
  "additionalProperties": false
}
`;

function ContractPicker({
  label,
  hint,
  value,
  options,
  readOnly,
  inputCls,
  onChange,
  onContractsChanged,
}: {
  label: string;
  hint: string;
  value: string | null;
  options: string[];
  readOnly: boolean;
  inputCls: string;
  onChange: (ref: string | null) => void;
  onContractsChanged?: () => void | Promise<void>;
}) {
  const str = value ?? "";
  // Keep a saved ref that isn't registered selectable instead of coercing it.
  const opts = str && !options.includes(str) ? [str, ...options] : options;
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("contract://");
  const [schemaText, setSchemaText] = useState(CONTRACT_SCHEMA_STARTER);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    setError(null);
    const name = newName.trim();
    if (!name || /\s/.test(name)) {
      setError("Name is required and cannot contain spaces.");
      return;
    }
    let schema: Record<string, unknown>;
    try {
      schema = JSON.parse(schemaText);
    } catch (e) {
      setError(`Schema is not valid JSON: ${(e as Error).message}`);
      return;
    }
    setBusy(true);
    try {
      const record = await createContract({ name, json_schema: schema });
      await onContractsChanged?.();
      onChange(record.name); // select it on the node right away
      setCreating(false);
      setNewName("contract://");
      setSchemaText(CONTRACT_SCHEMA_STARTER);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      {options.length > 0 ? (
        <select
          value={str}
          disabled={readOnly}
          onChange={(e) => onChange(e.target.value || null)}
          className={inputCls}
        >
          <option value="">(none)</option>
          {opts.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          value={str}
          placeholder="contract name"
          disabled={readOnly}
          onChange={(e) => onChange(e.target.value || null)}
          className={inputCls}
        />
      )}
      <span className="mt-1 block text-xs font-normal text-muted">{hint}</span>
    </label>

    {/* Define contracts without leaving the dialog: arbitrary JSON Schema,
        registered versioned in the deployment and selected here on create. */}
    {!readOnly && !creating && (
      <button
        type="button"
        onClick={() => setCreating(true)}
        className="mt-1.5 text-xs font-medium text-accent hover:underline"
      >
        New contract…
      </button>
    )}
    {creating && (
      <div className="mt-2 space-y-2 rounded-lg border border-border p-2.5">
        <label className="block text-xs">
          <span className="mb-1 block font-medium">Name</span>
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className={`${inputCls} font-mono`}
          />
        </label>
        <label className="block text-xs">
          <span className="mb-1 block font-medium">JSON Schema</span>
          <CodeEditor
            value={schemaText}
            language="json"
            height="170px"
            onChange={setSchemaText}
          />
          <span className="mt-1 block font-normal text-muted">
            Any JSON Schema works — payloads are validated against it exactly.
            Re-using an existing name registers the next version.
          </span>
        </label>
        {error && (
          <p className="rounded bg-red-500/10 px-2 py-1 text-xs text-red-700 dark:text-red-400">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setCreating(false)}
            className="rounded-lg border border-border px-2.5 py-1 text-xs font-medium"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={create}
            disabled={busy}
            className="rounded-lg bg-accent px-2.5 py-1 text-xs font-medium text-accent-fg disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create & select"}
          </button>
        </div>
      </div>
    )}
    </div>
  );
}

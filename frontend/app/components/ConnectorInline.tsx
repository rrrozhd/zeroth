"use client";

import { useState } from "react";
import { ApiErrorNote, Button, ErrorBox, Field, fieldInput, Input } from "@/app/components/ui";
import {
  BACKENDS,
  backendSpec,
  buildParams,
  REF_RE,
  requiredParamsOk,
  seedParamValues,
  type BackendSpec,
} from "@/app/lib/connectorBackends";
import {
  createConnector,
  errMsg,
  testConnector,
  updateConnector,
  type ConnectorSummary,
  type ConnectorTestResponse,
} from "@/app/lib/api";

/** The param inputs for one backend — shared between the Connectors page's
    form (two columns) and the inline node-settings panel (one column). */
export function ConnectorParamFields({
  spec,
  values,
  onChange,
  reenterSecrets = false,
  columns = 2,
}: {
  spec: BackendSpec;
  values: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  /** Editing an existing connector: stored secrets are masked, so the full
      value must be typed again. */
  reenterSecrets?: boolean;
  columns?: 1 | 2;
}) {
  return (
    <div className={columns === 2 ? "grid gap-4 sm:grid-cols-2" : "grid gap-3"}>
      {spec.fields.map((f) => (
        <Field
          key={f.key}
          label={f.required ? `${f.label} *` : f.label}
          hint={
            reenterSecrets && f.kind === "secret"
              ? "Secrets aren't shown — re-enter the full value."
              : f.hint
          }
        >
          <Input
            value={values[f.key] ?? ""}
            type={f.kind === "number" ? "number" : f.kind === "secret" ? "password" : "text"}
            autoComplete={f.kind === "secret" ? "off" : undefined}
            placeholder={f.placeholder}
            onChange={(e) => onChange({ ...values, [f.key]: e.target.value })}
            className={f.kind === "secret" ? "font-mono" : undefined}
          />
        </Field>
      ))}
    </div>
  );
}

function TestResultLine({ result }: { result: ConnectorTestResponse | { error: string } }) {
  if ("error" in result) {
    return <span className="text-red-600 dark:text-red-400">Test failed: {result.error}</span>;
  }
  return result.ok ? (
    <span className="text-emerald-600 dark:text-emerald-400">
      Connection OK · {Math.round(result.latency_ms)} ms
      {result.detail ? ` · ${result.detail}` : ""}
    </span>
  ) : (
    <span className="text-red-600 dark:text-red-400">
      Connection failed · {Math.round(result.latency_ms)} ms
      {result.detail ? ` · ${result.detail}` : ""}
    </span>
  );
}

function InlineFormError({ message }: { message: string }) {
  return message.startsWith("403") ? (
    <ApiErrorNote error={message} />
  ) : (
    <ErrorBox message={message} />
  );
}

/** Connector settings that live directly under a node's Connector dropdown:
    a glanceable summary of the selected backend with Test, an expandable
    edit form for runtime connectors, and a create form — so the graph
    author never has to leave the node dialog for the Connectors page. */
export function InlineConnectorSettings({
  selectedRef,
  connectors,
  readOnly = false,
  onSelectRef,
  onChanged,
}: {
  selectedRef: string;
  connectors: ConnectorSummary[];
  readOnly?: boolean;
  /** Point the node's connector_ref at a freshly created connector. */
  onSelectRef: (ref: string) => void;
  /** Re-fetch the deployment's connector list after a create/update. */
  onChanged?: () => void | Promise<void>;
}) {
  const selected = connectors.find((c) => c.ref === selectedRef) ?? null;

  const [open, setOpen] = useState<"none" | "edit" | "create">("none");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  // Both keyed by connector ref: a probe can run up to 5s server-side, and a
  // result that resolves after the user switches the dropdown must not paint
  // itself under the newly selected connector's card.
  const [testingRef, setTestingRef] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{
    ref: string;
    result: ConnectorTestResponse | { error: string };
  } | null>(null);

  // Edit form (seeded when the panel opens).
  const [editBackend, setEditBackend] = useState(BACKENDS[0].value);
  const [editValues, setEditValues] = useState<Record<string, string>>({});

  // Create form.
  const [createRef, setCreateRef] = useState("");
  const [createBackend, setCreateBackend] = useState(BACKENDS[0].value);
  const [createValues, setCreateValues] = useState<Record<string, string>>({});

  // Switching connectors in the dropdown drops stale edit/test state
  // (state-during-render pattern instead of an effect).
  const [seenRef, setSeenRef] = useState(selectedRef);
  if (seenRef !== selectedRef) {
    setSeenRef(selectedRef);
    if (open === "edit") setOpen("none");
    setTestResult(null);
    setFormError(null);
  }

  async function runTest() {
    if (!selected) return;
    const ref = selected.ref;
    setTestingRef(ref);
    setTestResult(null);
    try {
      setTestResult({ ref, result: await testConnector(ref) });
    } catch (e) {
      setTestResult({ ref, result: { error: errMsg(e) } });
    } finally {
      setTestingRef((t) => (t === ref ? null : t));
    }
  }

  function openEdit() {
    if (!selected) return;
    const bt = selected.backend_type ?? BACKENDS[0].value;
    setEditBackend(bt);
    setEditValues(seedParamValues(backendSpec(bt), selected.params ?? {}));
    setFormError(null);
    setOpen("edit");
  }

  async function saveEdit() {
    if (!selected) return;
    setBusy(true);
    setFormError(null);
    try {
      const spec = backendSpec(editBackend);
      await updateConnector(selected.ref, {
        backend_type: editBackend,
        params: buildParams(spec, editValues),
      });
      await onChanged?.();
      // The old probe result described the pre-edit config.
      setTestResult(null);
      setOpen("none");
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveCreate() {
    const ref = createRef.trim();
    setBusy(true);
    setFormError(null);
    try {
      const spec = backendSpec(createBackend);
      await createConnector({
        ref,
        backend_type: createBackend,
        params: buildParams(spec, createValues),
      });
      await onChanged?.();
      onSelectRef(ref);
      setOpen("none");
      setCreateRef("");
      setCreateValues({});
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const editSpec = backendSpec(editBackend);
  const createSpec = backendSpec(createBackend);
  const createRefOk = REF_RE.test(createRef.trim());
  const isRuntime = selected?.source === "runtime";

  return (
    <div
      className="mt-2 space-y-2"
      onKeyDown={(e) => {
        // Escape closes the inline form first, not the whole node dialog —
        // half-typed connector params shouldn't be lost to the dialog's
        // Escape handler. That handler listens on document, the same node
        // React delegates to (App Router hydrates the document), so plain
        // stopPropagation can't shield it; stopImmediatePropagation works
        // because the dialog's listener registers later (on mount).
        if (e.key === "Escape" && open !== "none") {
          e.stopPropagation();
          e.nativeEvent.stopImmediatePropagation();
          setOpen("none");
        }
      }}
    >
      {!selected && selectedRef !== "" && connectors.length > 0 && (
        <p className="rounded-lg border border-amber-300/70 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:border-amber-900/60 dark:text-amber-400">
          <span className="font-mono">{selectedRef}</span>
          {" isn't registered on this deployment — runs can't resolve it. Pick a registered connector or create one below."}
        </p>
      )}
      {selected && (
        <div className="rounded-lg border border-border bg-surface px-3 py-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0 text-xs text-muted">
              <span className="font-medium text-foreground">
                {selected.backend_type
                  ? backendSpec(selected.backend_type).label
                  : selected.backend}
              </span>
              {" · "}
              {selected.scope}
              {" · "}
              {selected.source}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <Button size="sm" onClick={runTest} disabled={testingRef !== null}>
                {testingRef === selected.ref ? "Testing…" : "Test"}
              </Button>
              {isRuntime && !readOnly && (
                <Button size="sm" onClick={() => (open === "edit" ? setOpen("none") : openEdit())}>
                  {open === "edit" ? "Close" : "Settings"}
                </Button>
              )}
            </div>
          </div>

          {testResult && testResult.ref === selected.ref && (
            <div className="mt-1.5 text-xs">
              <TestResultLine result={testResult.result} />
            </div>
          )}

          {!isRuntime && (
            <p className="mt-1.5 text-xs text-muted">
              env-sourced — configured in deployment settings, read-only here.
            </p>
          )}

          {open === "edit" && (
            <div className="mt-3 space-y-3 border-t border-border pt-3">
              <Field label="Backend">
                <select
                  value={editBackend}
                  onChange={(e) => {
                    setEditBackend(e.target.value);
                    setEditValues({});
                    setFormError(null);
                  }}
                  className={fieldInput}
                >
                  {BACKENDS.map((b) => (
                    <option key={b.value} value={b.value}>
                      {b.label}
                    </option>
                  ))}
                </select>
              </Field>
              <ConnectorParamFields
                spec={editSpec}
                values={editValues}
                onChange={setEditValues}
                reenterSecrets
                columns={1}
              />
              <Button
                variant="primary"
                size="sm"
                onClick={saveEdit}
                disabled={busy || !requiredParamsOk(editSpec, editValues)}
              >
                {busy ? "Saving…" : "Save connector"}
              </Button>
              {formError && <InlineFormError message={formError} />}
            </div>
          )}
        </div>
      )}

      {!readOnly &&
        (open === "create" ? (
          <div className="space-y-3 rounded-lg border border-border bg-surface px-3 py-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold">New connector</span>
              <Button size="sm" variant="ghost" onClick={() => setOpen("none")}>
                Cancel
              </Button>
            </div>
            <Field label="Ref" hint="lowercase letters, digits, _ and - only">
              <Input
                value={createRef}
                onChange={(e) => setCreateRef(e.target.value)}
                placeholder="docs_store"
                className="font-mono"
              />
            </Field>
            <Field label="Backend" hint={createSpec.desc}>
              <select
                value={createBackend}
                onChange={(e) => {
                  setCreateBackend(e.target.value);
                  setCreateValues({});
                  setFormError(null);
                }}
                className={fieldInput}
              >
                {BACKENDS.map((b) => (
                  <option key={b.value} value={b.value}>
                    {b.label}
                  </option>
                ))}
              </select>
            </Field>
            <ConnectorParamFields
              spec={createSpec}
              values={createValues}
              onChange={setCreateValues}
              columns={1}
            />
            <Button
              variant="primary"
              size="sm"
              onClick={saveCreate}
              disabled={busy || !createRefOk || !requiredParamsOk(createSpec, createValues)}
            >
              {busy ? "Creating…" : "Create & use"}
            </Button>
            {createRef.trim() !== "" && !createRefOk && (
              <p className="text-xs text-red-600 dark:text-red-400">
                Ref must be lowercase letters, digits, _ or -.
              </p>
            )}
            {formError && <InlineFormError message={formError} />}
          </div>
        ) : (
          <button
            type="button"
            onClick={() => {
              setFormError(null);
              setOpen("create");
            }}
            className="text-xs font-medium text-accent hover:underline"
          >
            + New connector
          </button>
        ))}
    </div>
  );
}

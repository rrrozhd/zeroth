"use client";

import { useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  fieldInput,
  Input,
  NotConnected,
  PageHeader,
  Skeleton,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  createConnector,
  deleteConnector,
  errMsg,
  listConnectors,
  testConnector,
  updateConnector,
  type ConnectorSummary,
  type ConnectorTestResponse,
} from "@/app/lib/api";

// --- Backend catalogue: the runtime-creatable connector backends and the
// param fields each one accepts. Mirrors the server-side param models.
type ParamField = {
  key: string;
  label: string;
  required?: boolean;
  kind?: "text" | "number" | "secret";
  /** Comma-separated input coerced into a string[] in the request body. */
  list?: boolean;
  placeholder?: string;
  hint?: string;
};

type BackendSpec = { value: string; label: string; desc: string; fields: ParamField[] };

const REDIS_FIELDS: ParamField[] = [
  {
    key: "url",
    label: "URL",
    required: true,
    kind: "secret",
    placeholder: "redis://:password@host:6379/0",
  },
  { key: "key_prefix", label: "Key prefix" },
];

const BACKENDS: BackendSpec[] = [
  {
    value: "pgvector",
    label: "Postgres (pgvector)",
    desc: "Vector similarity search over a Postgres table with the pgvector extension.",
    fields: [
      {
        key: "dsn",
        label: "DSN",
        required: true,
        kind: "secret",
        placeholder: "postgresql://user:password@host:5432/db",
      },
      { key: "table_name", label: "Table name" },
      { key: "embedding_model", label: "Embedding model" },
      { key: "embedding_dimensions", label: "Embedding dimensions", kind: "number" },
    ],
  },
  {
    value: "chroma",
    label: "ChromaDB",
    desc: "Vector search against a running Chroma server.",
    fields: [
      { key: "host", label: "Host", required: true, placeholder: "localhost" },
      { key: "port", label: "Port", kind: "number", placeholder: "8000" },
      { key: "collection_prefix", label: "Collection prefix" },
    ],
  },
  {
    value: "elasticsearch",
    label: "Elasticsearch",
    desc: "Full-text and vector retrieval against an Elasticsearch cluster.",
    fields: [
      {
        key: "hosts",
        label: "Hosts",
        required: true,
        list: true,
        placeholder: "http://localhost:9200",
        hint: "Comma-separated list.",
      },
      { key: "index_prefix", label: "Index prefix" },
    ],
  },
  {
    value: "redis_kv",
    label: "Redis (key-value)",
    desc: "Key-value memory store backed by Redis.",
    fields: REDIS_FIELDS,
  },
  {
    value: "redis_thread",
    label: "Redis (thread)",
    desc: "Conversation-thread memory backed by Redis.",
    fields: REDIS_FIELDS,
  },
];

function backendSpec(value: string): BackendSpec {
  return BACKENDS.find((b) => b.value === value) ?? BACKENDS[0];
}

/** Build the params object for the request body: blank optional fields are
    omitted, numbers coerced, comma-lists split into arrays. */
function buildParams(
  spec: BackendSpec,
  values: Record<string, string>,
): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const f of spec.fields) {
    const raw = (values[f.key] ?? "").trim();
    if (!raw) continue; // omit blanks — server defaults apply
    if (f.kind === "number") params[f.key] = Number(raw);
    else if (f.list) params[f.key] = raw.split(",").map((s) => s.trim()).filter(Boolean);
    else params[f.key] = raw;
  }
  return params;
}

const REF_RE = /^[a-z0-9_-]+$/;

export default function ConnectorsPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(listConnectors, []);

  // --- Edit state lives at page level so the list's Edit buttons can load a
  // row into the form card below.
  const [editing, setEditing] = useState<ConnectorSummary | null>(null);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Connectors"
        subtitle="Memory & retrieval backends this deployment can resolve."
        actions={
          connected ? (
            <Button onClick={() => reload()} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </Button>
          ) : undefined
        }
      />
      {!connected ? (
        <NotConnected />
      ) : (
        <>
          <p className="text-sm text-muted">
            <span className="font-medium text-foreground">env</span> connectors come from
            deployment settings and are read-only here;{" "}
            <span className="font-medium text-foreground">runtime</span> connectors are
            stored in the platform DB and usable immediately.
          </p>

          {error && <ApiErrorNote error={error} />}
          {loading && !data && <Skeleton rows={4} />}
          {data && data.length === 0 && (
            <Empty>No connectors registered — add one below.</Empty>
          )}
          {data && data.length > 0 && (
            <ConnectorList rows={data} onEdit={setEditing} onChanged={reload} />
          )}

          <ConnectorForm
            editing={editing}
            onCancelEdit={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              reload();
            }}
          />
        </>
      )}
    </div>
  );
}

function ConnectorList({
  rows,
  onEdit,
  onChanged,
}: {
  rows: ConnectorSummary[];
  onEdit: (row: ConnectorSummary) => void;
  onChanged: () => void;
}) {
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, ConnectorTestResponse | { error: string }>
  >({});
  const [deletingRef, setDeletingRef] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ ref: string; msg: string } | null>(null);

  async function test(ref: string) {
    setTesting(ref);
    setTestResults((r) => {
      const rest = { ...r };
      delete rest[ref];
      return rest;
    });
    try {
      const res = await testConnector(ref);
      setTestResults((r) => ({ ...r, [ref]: res }));
    } catch (e) {
      setTestResults((r) => ({ ...r, [ref]: { error: errMsg(e) } }));
    } finally {
      setTesting(null);
    }
  }

  async function remove(ref: string) {
    if (!window.confirm(`Delete connector "${ref}"? Nodes referencing it will stop resolving.`))
      return;
    setDeletingRef(ref);
    setRowError(null);
    try {
      await deleteConnector(ref);
      onChanged();
    } catch (e) {
      setRowError({ ref, msg: errMsg(e) });
    } finally {
      setDeletingRef(null);
    }
  }

  return (
    <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface">
      {rows.map((c) => {
        const isRuntime = c.source === "runtime";
        const result = testResults[c.ref];
        return (
          <li key={c.ref} className="px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-semibold">{c.ref}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      isRuntime
                        ? "bg-accent/10 text-accent"
                        : "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400"
                    }`}
                  >
                    {c.source}
                  </span>
                  <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted">
                    {c.scope}
                  </span>
                </div>
                <div className="mt-0.5 text-xs text-muted">
                  {c.backend} · {c.connector_type}
                  {c.backend_type && (
                    <span className="ml-1 font-mono">({c.backend_type})</span>
                  )}
                </div>
                {isRuntime && c.params && (
                  <div className="mt-1 truncate font-mono text-xs text-muted">
                    {JSON.stringify(c.params)}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button size="sm" onClick={() => test(c.ref)} disabled={testing !== null}>
                  {testing === c.ref ? "Testing…" : "Test"}
                </Button>
                {isRuntime && (
                  <>
                    <Button size="sm" onClick={() => onEdit(c)}>
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => remove(c.ref)}
                      disabled={deletingRef === c.ref}
                    >
                      {deletingRef === c.ref ? "Deleting…" : "Delete"}
                    </Button>
                  </>
                )}
              </div>
            </div>
            {result && (
              <div className="mt-2 text-xs">
                {"error" in result ? (
                  <span className="text-red-600 dark:text-red-400">Test failed: {result.error}</span>
                ) : result.ok ? (
                  <span className="text-emerald-600 dark:text-emerald-400">
                    Connection OK · {Math.round(result.latency_ms)} ms
                    {result.detail ? ` · ${result.detail}` : ""}
                  </span>
                ) : (
                  <span className="text-red-600 dark:text-red-400">
                    Connection failed · {Math.round(result.latency_ms)} ms
                    {result.detail ? ` · ${result.detail}` : ""}
                  </span>
                )}
              </div>
            )}
            {rowError?.ref === c.ref && (
              <div className="mt-2">
                <ErrorBox message={rowError.msg} />
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ConnectorForm({
  editing,
  onCancelEdit,
  onSaved,
}: {
  editing: ConnectorSummary | null;
  onCancelEdit: () => void;
  onSaved: () => void;
}) {
  const [backendType, setBackendType] = useState(BACKENDS[0].value);
  const [ref, setRef] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Track which row is loaded so a click on another Edit button re-seeds the
  // form (state-during-render pattern instead of an effect).
  const [loadedRef, setLoadedRef] = useState<string | null>(null);
  if (editing && editing.ref !== loadedRef) {
    setLoadedRef(editing.ref);
    setBackendType(editing.backend_type ?? BACKENDS[0].value);
    setRef(editing.ref);
    const spec = backendSpec(editing.backend_type ?? BACKENDS[0].value);
    const seeded: Record<string, string> = {};
    for (const f of spec.fields) {
      // Secrets come back masked — don't round-trip the mask into a PUT.
      if (f.kind === "secret") continue;
      const v = (editing.params ?? {})[f.key];
      if (v === undefined || v === null) continue;
      seeded[f.key] = Array.isArray(v) ? v.join(", ") : String(v);
    }
    setValues(seeded);
    setFormError(null);
    setSaved(false);
  }
  if (!editing && loadedRef !== null) {
    setLoadedRef(null);
    setRef("");
    setValues({});
    setFormError(null);
  }

  const spec = backendSpec(backendType);
  const refOk = editing ? true : REF_RE.test(ref);
  const requiredOk = spec.fields
    .filter((f) => f.required)
    .every((f) => (values[f.key] ?? "").trim() !== "");

  async function save() {
    setBusy(true);
    setFormError(null);
    setSaved(false);
    try {
      const params = buildParams(spec, values);
      if (editing) {
        await updateConnector(editing.ref, { backend_type: backendType, params });
      } else {
        await createConnector({ ref: ref.trim(), backend_type: backendType, params });
      }
      setRef("");
      setValues({});
      setSaved(true);
      onSaved();
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={editing ? `Edit connector: ${editing.ref}` : "Add connector"}
      actions={
        editing ? (
          <Button size="sm" variant="ghost" onClick={onCancelEdit}>
            Cancel edit
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-4">
        <Field label="Backend">
          <select
            value={backendType}
            onChange={(e) => {
              setBackendType(e.target.value);
              setValues({});
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
          <span className="mt-1 block text-xs text-muted">{spec.desc}</span>
        </Field>

        {!editing && (
          <Field label="Ref" hint="lowercase letters, digits, _ and - only">
            <Input
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="docs_store"
              className="font-mono"
            />
          </Field>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          {spec.fields.map((f) => (
            <Field
              key={f.key}
              label={f.required ? `${f.label} *` : f.label}
              hint={
                editing && f.kind === "secret"
                  ? "Secrets aren't shown — re-enter the full value."
                  : f.hint
              }
            >
              <Input
                value={values[f.key] ?? ""}
                type={f.kind === "number" ? "number" : f.kind === "secret" ? "password" : "text"}
                autoComplete={f.kind === "secret" ? "off" : undefined}
                placeholder={f.placeholder}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                className={f.kind === "secret" ? "font-mono" : undefined}
              />
            </Field>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            onClick={save}
            disabled={busy || !refOk || !requiredOk || (!editing && !ref.trim())}
          >
            {busy ? "Saving…" : editing ? "Save changes" : "Add connector"}
          </Button>
          {!editing && ref.trim() !== "" && !refOk && (
            <span className="text-xs text-red-600 dark:text-red-400">
              Ref must be lowercase letters, digits, _ or -.
            </span>
          )}
        </div>

        {saved && (
          <p className="text-sm text-emerald-600 dark:text-emerald-400">
            Saved — now available in the retrieval node&apos;s Connector dropdown.
          </p>
        )}
        {formError &&
          (formError.startsWith("403") ? (
            <ApiErrorNote error={formError} />
          ) : (
            <ErrorBox message={formError} />
          ))}
      </div>
    </Card>
  );
}

// --- Backend catalogue: the runtime-creatable connector backends and the
// param fields each one accepts. Mirrors the server-side param models.
// Shared by the Connectors page and the node inspector's inline settings.

export type ParamField = {
  key: string;
  label: string;
  required?: boolean;
  kind?: "text" | "number" | "secret";
  /** Comma-separated input coerced into a string[] in the request body. */
  list?: boolean;
  placeholder?: string;
  hint?: string;
};

export type BackendSpec = { value: string; label: string; desc: string; fields: ParamField[] };

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

export const BACKENDS: BackendSpec[] = [
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

export function backendSpec(value: string): BackendSpec {
  return BACKENDS.find((b) => b.value === value) ?? BACKENDS[0];
}

/** Build the params object for the request body: blank optional fields are
    omitted, numbers coerced, comma-lists split into arrays. */
export function buildParams(
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

/** Seed form values from a connector's stored params. Secrets come back
    masked from the API, so they're skipped — never round-trip a mask. */
export function seedParamValues(
  spec: BackendSpec,
  params: Record<string, unknown>,
): Record<string, string> {
  const seeded: Record<string, string> = {};
  for (const f of spec.fields) {
    if (f.kind === "secret") continue;
    const v = params[f.key];
    if (v === undefined || v === null) continue;
    const s = Array.isArray(v) ? v.join(", ") : String(v);
    // Non-secret fields can still carry masked credentials (e.g. hosts URLs
    // come back as "http://***@host") — those must be re-entered too.
    if (s === "***" || s.includes("://***@")) continue;
    seeded[f.key] = s;
  }
  return seeded;
}

export function requiredParamsOk(
  spec: BackendSpec,
  values: Record<string, string>,
): boolean {
  return spec.fields
    .filter((f) => f.required)
    .every((f) => (values[f.key] ?? "").trim() !== "");
}

export const REF_RE = /^[a-z0-9_-]+$/;
